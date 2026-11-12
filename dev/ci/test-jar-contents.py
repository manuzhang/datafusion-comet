#!/usr/bin/env python3
#
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

"""Test dev/ensure-jars-have-correct-contents.sh against real ZIP files.

The checker reads pom.xml and dev/licenses/LICENSE-binary.txt relative to itself. Each test
runs a copy of it in a temporary tree whose pom pins the version that LICENSE-binary.txt names,
so only test_license_matches_pom reads the real pom. dev/generate-binary-license.py --check
covers the section of the LICENSE generated from native/Cargo.lock.
"""

from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest
import warnings
import zipfile

ROOT = Path(__file__).resolve().parents[2]
CHECKER = ROOT / "dev/ensure-jars-have-correct-contents.sh"
LICENSE = (ROOT / "dev/licenses/LICENSE-binary.txt").read_bytes()
NATIVE_LIB = "org/apache/comet/linux/amd64/libcomet.so"


PROTOBUF = re.search(
    rb"^The Comet JAR bundles protobuf-java (\S+) \(BSD-3-Clause\)\.$", LICENSE, re.M
).group(1).decode()


def pom(*protobuf_versions):
    properties = "".join(
        f"    <protobuf.version>{version}</protobuf.version>\n" for version in protobuf_versions)
    return f"<project>\n  <properties>\n{properties}  </properties>\n</project>\n"


def without_line(text, prefix):
    lines = text.splitlines(keepends=True)
    kept = [line for line in lines if not line.startswith(prefix)]
    assert len(kept) == len(lines) - 1, prefix
    return b"".join(kept)


class JarContents(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.checker = self.root / "dev" / CHECKER.name
        (self.root / "dev/licenses").mkdir(parents=True)
        shutil.copy(CHECKER, self.checker)
        (self.root / "dev/licenses/LICENSE-binary.txt").write_bytes(LICENSE)
        self.pom(pom(PROTOBUF))
        self.main = self.jar("main.jar")

    def pom(self, text):
        (self.root / "pom.xml").write_text(text)

    def jar(self, name, entry=None, native=True, licenses=(LICENSE,)):
        path = self.root / name
        with warnings.catch_warnings():
            # Duplicate entries are the point of one test; zipfile warns about them.
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(path, "w") as archive:
                if native:
                    archive.writestr(NATIVE_LIB, b"")
                for text in licenses:
                    archive.writestr("META-INF/LICENSE", text)
                if entry:
                    archive.writestr(entry, "// source\n")
        return str(path)

    def check(self, *args, checker=None):
        return subprocess.run(
            ["bash", str(checker or self.checker), *args], capture_output=True, text=True)

    def passed(self, *args, checker=None):
        result = self.check(*args, checker=checker)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def failed(self, args, *messages):
        result = self.check(*args)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        for message in messages:
            self.assertIn(message, result.stdout)

    def rejected(self, args, entry):
        self.failed(args, "unexpected contents", entry)

    def check_source_entry(self, entry):
        artifact = self.jar("payload.jar", entry)
        self.rejected([artifact], entry)
        self.passed(self.main, artifact)
        # Permissions must not leak into the main list, even for the same path.
        self.rejected([self.main + ";" + artifact, artifact], entry)
        self.rejected([artifact, artifact], entry)

    def test_module_descriptor(self):
        self.check_source_entry("module-info.java")

    def test_plugins_source(self):
        self.check_source_entry("org/apache/spark/Plugins.scala")

    def test_row_partition_source(self):
        self.check_source_entry("org/apache/spark/shuffle/sort/RowPartition.scala")

    def test_unrelated_source_rejected_in_both_roles(self):
        entry = "Unexpected.java"
        artifact = self.jar("unexpected.jar", entry)
        self.rejected([artifact], entry)
        self.rejected([self.main, artifact], entry)

    def test_license_matches_pom(self):
        """Run the checker in place, so it reads the repository's pom.xml.

        A bump of protobuf.version fails here, ahead of the build's own JAR check, until
        LICENSE-binary.txt names the new version and its text.
        """
        self.assertIn("Binary LICENSE matches", self.passed(self.main, checker=CHECKER))

    def test_main_and_sources_licenses_match(self):
        sources = self.jar("sources.jar")
        output = self.passed(self.main, sources)
        self.assertEqual(output.count("Binary LICENSE matches"), 2, output)

    def test_large_jar_listing(self):
        # A listing far beyond a pipe buffer, with libcomet first, so a grep -q on a pipe
        # would exit before the writer finishes and, under pipefail, skip the LICENSE check.
        artifact = self.root / "large.jar"
        with zipfile.ZipFile(artifact, "w") as archive:
            archive.writestr(NATIVE_LIB, b"")
            archive.writestr("META-INF/LICENSE", LICENSE)
            for i in range(30000):
                archive.writestr(f"org/apache/comet/C{i}.class", b"")
        self.assertIn("Binary LICENSE matches", self.passed(str(artifact)))

    def test_jar_without_native_library_keeps_its_license(self):
        # Common and test-only jars carry no libcomet, so the binary LICENSE does not apply.
        common = self.jar("common.jar", native=False, licenses=())
        self.assertNotIn("Binary LICENSE", self.passed(common))

    def test_missing_or_duplicate_license(self):
        for licenses in [(), (LICENSE, LICENSE)]:
            with self.subTest(count=len(licenses)):
                artifact = self.jar("native.jar", licenses=licenses)
                self.failed([artifact], "Expected exactly one META-INF/LICENSE")

    def test_altered_license(self):
        cases = {
            "truncated": LICENSE[: len(LICENSE) // 2],
            # What 1.2.0-rc1 shipped: the Apache license alone.
            "apache only": (ROOT / "LICENSE.txt").read_bytes(),
            "no lz4_flex copyright": without_line(LICENSE, b"Copyright (c) 2020 Pascal Seitz"),
            "no AWS-LC copyright": without_line(
                LICENSE, b"Copyright (c) 2015-2020 the fiat-crypto"),
            "no protobuf-java copyright": without_line(
                LICENSE, b"Copyright 2008 Google Inc.  All rights"),
        }
        for case, text in cases.items():
            with self.subTest(case):
                artifact = self.jar("native.jar", licenses=(text,))
                self.failed([artifact], "Binary LICENSE differs")

    def test_bundled_protobuf_version_drift(self):
        self.pom(pom("3.25.6"))
        self.failed([self.main], "Update LICENSE-binary-jvm.txt for bundled protobuf-java 3.25.6")

    def test_unreadable_protobuf_version(self):
        for case, versions in {"missing": (), "ambiguous": (PROTOBUF, "3.25.6")}.items():
            with self.subTest(case):
                self.pom(pom(*versions))
                self.failed([self.main], "Expected one protobuf.version property")

    def test_missing_sources_jar(self):
        missing = str(self.root / "missing-sources.jar")
        for skip in [None, "false"]:
            with self.subTest(skip=skip):
                self.failed([self.main, missing] + ([skip] if skip else []),
                            "Expected sources JAR does not exist")
        for skip in ["true", "TRUE"]:
            with self.subTest(skip=skip):
                self.assertIn("Skipping sources-JAR verification",
                              self.passed(self.main, missing, skip))

    def test_unlistable_jar(self):
        artifact = self.root / "corrupt.jar"
        artifact.write_bytes(b"not a zip")
        self.failed([str(artifact)], "Listing jar contents for file")


if __name__ == "__main__":
    unittest.main()
