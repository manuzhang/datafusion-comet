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

"""Generate dev/licenses/LICENSE-binary.txt, the LICENSE shipped in the Comet JARs.

The file is the Apache license, the hand-maintained dev/licenses/LICENSE-binary-jvm.txt for the
JVM dependencies shaded into the JAR, and a section generated with cargo-about for the crates
and C libraries linked into libcomet. Crate versions are left out, so the file changes only
when the set of crates or a license text does.

    cargo install cargo-about --version 0.9.3 --locked --features cli
    dev/generate-binary-license.py           # rewrite the file
    dev/generate-binary-license.py --check   # fail if the file is out of date
"""

import argparse
import difflib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
LICENSES = ROOT / "dev/licenses"
OUTPUT = LICENSES / "LICENSE-binary.txt"
RULE = "-" * 80 + "\n"

# Crates that vendor C code under terms cargo-about cannot recover in full from the crate's
# license expression. Their license files are reproduced whole instead.
VENDORED = {
    "aws-lc-sys": ("AWS-LC", ["LICENSE"]),
    "ring": ("code derived from BoringSSL", ["LICENSE", "LICENSE-BoringSSL", "LICENSE-other-bits"]),
}


def cargo_about():
    command = [
        "cargo", "about", "generate", "--frozen", "--fail", "--format", "json",
        "--manifest-path", str(ROOT / "native/core/Cargo.toml"),
        "--config", str(LICENSES / "about.toml"),
    ]
    result = subprocess.run(command, cwd=ROOT / "native", capture_output=True, text=True)
    if result.returncode != 0:
        sys.exit(f"cargo about failed. Is cargo-about installed and `cargo fetch` done?\n"
                 f"{result.stderr}")
    return json.loads(result.stdout)


def text_of(text):
    """A license text with normalized line endings and exactly one trailing newline."""
    return text.replace("\r\n", "\n").strip("\n") + "\n"


def crate_list(names):
    return "".join(f"  - {name}\n" for name in sorted(set(names)))


def native_sections(about):
    apache = []
    others = {}
    for entry in about["licenses"]:
        names = [used["crate"]["name"] for used in entry["used_by"]]
        if entry["id"] == "Apache-2.0":
            apache += names
            continue
        names = [name for name in names if name not in VENDORED]
        if names:
            key = (entry["id"], entry["name"], text_of(entry["text"]))
            others.setdefault(key, []).extend(names)

    yield ("The native Comet library (libcomet) in this JAR statically links the Rust crates and\n"
           "C libraries listed below. dev/generate-binary-license.py generates this list.\n\n"
           "These crates are under the Apache License 2.0 above:\n\n" + crate_list(apache))

    for (license_id, name, text), names in sorted(
            others.items(), key=lambda item: (item[0][0], sorted(set(item[1])), item[0][2])):
        yield f"These crates are under the {name} ({license_id}):\n\n{crate_list(names)}\n{text}"

    manifests = {crate["package"]["name"]: Path(crate["package"]["manifest_path"]).parent
                 for crate in about["crates"]}
    for crate, (what, files) in sorted(VENDORED.items()):
        if crate not in manifests:
            sys.exit(f"{crate} is no longer linked into libcomet. Remove it from VENDORED.")
        for name in files:
            text = text_of((manifests[crate] / name).read_text())
            yield f"The {crate} crate bundles {what}. Its {name} file:\n\n{text}"


def generate():
    parts = [text_of((ROOT / "LICENSE.txt").read_text()),
             text_of((LICENSES / "LICENSE-binary-jvm.txt").read_text())]
    parts += native_sections(cargo_about())
    return ("\n" + RULE + "\n").join(parts)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true",
                        help="fail if LICENSE-binary.txt is out of date, without writing it")
    args = parser.parse_args()
    generated = generate()
    if not args.check:
        OUTPUT.write_text(generated)
        return
    current = OUTPUT.read_text()
    if current != generated:
        sys.stdout.writelines(difflib.unified_diff(
            current.splitlines(keepends=True), generated.splitlines(keepends=True),
            "dev/licenses/LICENSE-binary.txt", "generated"))
        sys.exit("LICENSE-binary.txt is out of date. Run dev/generate-binary-license.py.")


if __name__ == "__main__":
    main()
