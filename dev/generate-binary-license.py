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

"""Generate the LICENSE and NOTICE shipped in the Comet JARs.

dev/licenses/LICENSE-binary.txt is the Apache license, the hand-maintained
dev/licenses/LICENSE-binary-jvm.txt for the JVM dependencies shaded into the JAR, and a section
generated with cargo-about for the crates and C libraries linked into libcomet.
dev/licenses/NOTICE-binary.txt is NOTICE.txt, the hand-maintained
dev/licenses/NOTICE-binary-bundled.txt, and the NOTICE file of every linked crate that has one.
Crate versions are left out, so the files change only when the set of crates, a license text
or a NOTICE does.

    cargo install cargo-about --version 0.9.3 --locked --features cli
    cargo fetch --locked --manifest-path native/Cargo.toml
    dev/generate-binary-license.py                  # rewrite both files
    dev/generate-binary-license.py --check          # fail if either file is out of date
    dev/generate-binary-license.py --check-crates   # fail if LICENSE-binary.txt does not list
                                                    # the crates cargo links; needs only cargo
"""

import argparse
import difflib
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
LICENSES = ROOT / "dev/licenses"
LICENSE = LICENSES / "LICENSE-binary.txt"
NOTICE = LICENSES / "NOTICE-binary.txt"
RULE = "-" * 80 + "\n"
# The release JARs carry libcomet for these targets only. Keep in step with about.toml.
TARGETS = ["x86_64-unknown-linux-gnu", "aarch64-unknown-linux-gnu"]

# Crates whose license files are reproduced whole, because cargo-about cannot recover their
# terms in full: they vendor C code under several licenses, or their one file holds several.
# Each maps to what the crate bundles and the files, relative to the crate.
WHOLE_FILES = {
    "aws-lc-rs": ("bindings to AWS-LC", ["LICENSE"]),
    "aws-lc-sys": ("AWS-LC", ["LICENSE"]),
    "brotli-decompressor": ("a port of Brotli", ["LICENSE"]),
    "libm": ("ports of musl and FreeBSD math routines", ["LICENSE.txt"]),
    "ring": ("code derived from BoringSSL", ["LICENSE", "LICENSE-BoringSSL", "LICENSE-other-bits"]),
}

# Crates published without a license file. Each maps to a copy of the license file in the
# crate's repository, at the commit the crate was published from, under dev/licenses/crates.
# A crate whose repository has no license file either is not listed here: it keeps the
# standard text of its license, and its authors are named next to it.
REPOSITORY_FILES = {
    "alloc-stdlib": "alloc-stdlib-LICENSE.txt",
    "base64-simd": "simd-LICENSE.txt",
    "vsimd": "simd-LICENSE.txt",
}


def cargo(*args):
    result = subprocess.run(["cargo", *args], cwd=ROOT / "native", capture_output=True, text=True)
    if result.returncode != 0:
        sys.exit(f"cargo {args[0]} failed. For `about`, is cargo-about installed and "
                 f"`cargo fetch` done?\n{result.stderr}")
    return result.stdout


def cargo_about():
    return json.loads(cargo(
        "about", "generate", "--frozen", "--fail", "--format", "json",
        "--manifest-path", str(ROOT / "native/core/Cargo.toml"),
        "--config", str(LICENSES / "about.toml")))


def text_of(text):
    """A license text with normalized line endings and exactly one trailing newline."""
    return text.replace("\r\n", "\n").strip("\n") + "\n"


def crate_list(names):
    return "".join(f"  - {name}\n" for name in sorted(set(names)))


def native_sections(about):
    packages = {crate["package"]["name"]: crate["package"] for crate in about["crates"]}
    for crate in list(WHOLE_FILES) + list(REPOSITORY_FILES):
        if crate not in packages:
            sys.exit(f"{crate} is no longer linked into libcomet. Remove it from this script.")

    apache = []
    others = {}
    for entry in about["licenses"]:
        names = [used["crate"]["name"] for used in entry["used_by"]]
        if entry["id"] == "Apache-2.0":
            apache += names
            continue
        names = [name for name in names if name not in WHOLE_FILES and name not in REPOSITORY_FILES]
        if not names:
            continue
        note = ""
        if entry["source_path"] is None:
            # cargo-about found no license file and supplied the standard text, which names
            # no copyright holder.
            note = "".join(
                f"{name} ships no license file. Its authors: "
                f"{', '.join(packages[name]['authors'])}.\n" for name in sorted(set(names)))
        key = (entry["id"], entry["name"], note, text_of(entry["text"]))
        others.setdefault(key, []).extend(names)

    yield ("The native Comet library (libcomet) in this JAR statically links the Rust crates and\n"
           "C libraries listed below. dev/generate-binary-license.py generates this list.\n\n"
           "These crates are under the Apache License 2.0 above:\n\n" + crate_list(apache))

    for (license_id, name, note, text), names in sorted(
            others.items(), key=lambda item: (item[0][0], sorted(set(item[1])), item[0][3])):
        yield (f"These crates are under the {name} ({license_id}):\n\n{crate_list(names)}\n"
               + (note + "\n" if note else "") + text)

    for crate, (what, files) in sorted(WHOLE_FILES.items()):
        root = Path(packages[crate]["manifest_path"]).parent
        for name in files:
            yield (f"The {crate} crate bundles {what}. Its {name} file:\n\n"
                   + text_of((root / name).read_text()))
    for crate, name in sorted(REPOSITORY_FILES.items()):
        yield (f"The {crate} crate ships no license file. The one in its repository:\n\n"
               + text_of((LICENSES / "crates" / name).read_text()))


def generate_license(about):
    parts = [text_of((ROOT / "LICENSE.txt").read_text()),
             text_of((LICENSES / "LICENSE-binary-jvm.txt").read_text())]
    parts += native_sections(about)
    return ("\n" + RULE + "\n").join(parts)


def generate_notice(about):
    """NOTICE.txt, the hand-maintained notices, and each distinct NOTICE among linked crates."""
    notices = {}
    for crate in about["crates"]:
        package = crate["package"]
        if package["source"] is None:
            continue  # A workspace member: NOTICE.txt already covers Comet itself.
        for path in sorted(Path(package["manifest_path"]).parent.glob("NOTICE*")):
            notices.setdefault(text_of(path.read_text()), []).append(package["name"])
    parts = [text_of((ROOT / "NOTICE.txt").read_text()),
             text_of((LICENSES / "NOTICE-binary-bundled.txt").read_text())]
    parts += [text for text, names in sorted(notices.items(), key=lambda item: sorted(item[1]))]
    return ("\n" + RULE + "\n").join(parts)


def listed_crates():
    """The crates LICENSE-binary.txt names, from its generated lists and section headings."""
    text = LICENSE.read_text()
    names = set(re.findall(r"^The (\S+) crate (?:bundles|ships) ", text, re.M))
    for block in re.findall(r"^These crates are under [^\n]*:\n\n((?:  - \S+\n)+)", text, re.M):
        names.update(re.findall(r"^  - (\S+)$", block, re.M))
    return names


def check_crates():
    linked = set()
    for target in TARGETS:
        tree = cargo("tree", "--locked", "-p", "datafusion-comet", "-e", "normal",
                     "--target", target, "--prefix", "none", "--format", "{p}")
        linked.update(line.split()[0] for line in tree.splitlines() if line.strip())
    listed = listed_crates()
    for message, names in [("linked into libcomet but not listed", linked - listed),
                           ("listed but no longer linked into libcomet", listed - linked)]:
        if names:
            print(f"Crates {message}: {', '.join(sorted(names))}")
    if linked != listed:
        sys.exit("LICENSE-binary.txt is out of date. Run dev/generate-binary-license.py.")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true",
                       help="fail if either file is out of date, without writing it")
    group.add_argument("--check-crates", action="store_true",
                       help="fail if LICENSE-binary.txt does not list exactly the linked crates")
    args = parser.parse_args()
    if args.check_crates:
        check_crates()
        return
    about = cargo_about()
    stale = False
    for path, generated in [(LICENSE, generate_license(about)), (NOTICE, generate_notice(about))]:
        if not args.check:
            path.write_text(generated)
        elif not path.exists() or path.read_text() != generated:
            current = path.read_text() if path.exists() else ""
            sys.stdout.writelines(difflib.unified_diff(
                current.splitlines(keepends=True), generated.splitlines(keepends=True),
                str(path.relative_to(ROOT)), "generated"))
            stale = True
    if stale:
        sys.exit("The binary LICENSE or NOTICE is out of date. "
                 "Run dev/generate-binary-license.py.")


if __name__ == "__main__":
    main()
