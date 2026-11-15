"""Wheel loading, structural validation, and member digests.

Never extracts members: all bytes are read in memory (streamed) for hashing.
A wheel that fails structural validation yields WheelError and no mapping is
trusted from it.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import os
import re
import zipfile
from dataclasses import dataclass, field


class WheelError(Exception):
    """The wheel cannot be trusted as an artifact anchor."""


_WHEEL_NAME_RE = re.compile(
    r"^(?P<name>[A-Za-z0-9](?:[A-Za-z0-9._]*[A-Za-z0-9])?)-"
    r"(?P<version>[^-]+)"
    r"(?:-(?P<build>[^-]+))?-"
    r"(?P<pytag>[^-]+)-(?P<abitag>[^-]+)-(?P<plattag>[^-]+)\.whl$"
)


def _member_path_unsafe(name: str) -> bool:
    if not name or name.startswith("/") or name.startswith("\\"):
        return True
    if "\\" in name:  # backslash separators are ambiguous across platforms
        return True
    drive = re.match(r"^[A-Za-z]:", name)
    if drive:
        return True
    # One trailing slash denotes a directory, not an empty path component.
    # All other empty, dot and traversal components remain ambiguous/unsafe.
    path = name[:-1] if name.endswith("/") else name
    return any(part in (".", "..", "") for part in path.split("/"))


def _b64_sha256(digest: bytes) -> str:
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


@dataclass
class WheelInfo:
    path: str
    filename: str
    name: str
    version: str
    wheel_sha256: str
    members: dict = field(default_factory=dict)  # arcname -> sha256 hex
    record_digests: dict = field(default_factory=dict)  # arcname -> b64 digest from RECORD
    record_entries_without_digest: list = field(default_factory=list)
    data_members: list = field(default_factory=list)  # .data/ relocation members
    dist_info: str = ""
    warnings: list = field(default_factory=list)

    def members_matching(self, sha256_hex: str) -> list:
        return sorted(n for n, h in self.members.items() if h == sha256_hex)


def _hash_stream(f) -> str:
    h = hashlib.sha256()
    while True:
        chunk = f.read(1 << 20)
        if not chunk:
            break
        h.update(chunk)
    return h.hexdigest()


def load_wheel(path: str) -> WheelInfo:
    if not os.path.isfile(path):
        raise WheelError("wheel file not found: %s" % path)
    filename = os.path.basename(path)
    m = _WHEEL_NAME_RE.match(filename)
    if not m:
        raise WheelError("filename does not parse as a wheel: %s" % filename)

    with open(path, "rb") as f:
        wheel_sha256 = _hash_stream(f)

    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as e:
        raise WheelError("not a readable zip archive: %s" % e)

    with zf:
        bad = zf.testzip()
        if bad is not None:
            raise WheelError("corrupt zip member: %s" % bad)

        names = zf.namelist()
        if len(names) != len(set(names)):
            dupes = sorted({n for n in names if names.count(n) > 1})
            raise WheelError("duplicate zip members: %s" % ", ".join(dupes[:5]))

        unsafe = [n for n in names if _member_path_unsafe(n)]
        if unsafe:
            raise WheelError(
                "unsafe member paths (absolute/traversal/backslash): %s"
                % ", ".join(unsafe[:5])
            )

        dist_infos = sorted(
            {n.split("/")[0] for n in names if n.endswith(".dist-info/METADATA")}
        )
        if len(dist_infos) != 1:
            raise WheelError(
                "expected exactly one .dist-info with METADATA, found %d"
                % len(dist_infos)
            )
        dist_info = dist_infos[0]
        record_name = dist_info + "/RECORD"
        if record_name not in names:
            raise WheelError("missing %s" % record_name)

        # Identity must be unambiguous: filename, dist-info dir, and
        # METADATA Name/Version must agree, or no mapping is trusted.
        expected_di = "%s-%s.dist-info" % (
            m.group("name").replace("-", "_"),
            m.group("version"),
        )
        di_name, separator, di_version = dist_info[:-len(".dist-info")].rpartition("-")
        if (not separator or di_version != m.group("version") or
                re.sub(r"[-_.]+", "-", di_name).lower() !=
                re.sub(r"[-_.]+", "-", m.group("name")).lower()):
            raise WheelError(
                "dist-info dir %r conflicts with filename-derived %r"
                % (dist_info, expected_di)
            )
        try:
            meta = zf.read(dist_info + "/METADATA").decode("utf-8", "replace")
        except KeyError:
            raise WheelError("missing %s/METADATA" % dist_info)
        meta_name = meta_version = None
        for line in meta.splitlines():
            if line.startswith("Name:"):
                meta_name = line.split(":", 1)[1].strip()
            elif line.startswith("Version:"):
                meta_version = line.split(":", 1)[1].strip()
            if line.strip() == "":
                break  # end of headers
        if meta_name is None or meta_version is None:
            raise WheelError("METADATA lacks Name/Version headers")
        if re.sub(r"[-_.]+", "-", meta_name).lower() != \
                re.sub(r"[-_.]+", "-", m.group("name")).lower():
            raise WheelError(
                "METADATA Name %r conflicts with filename name %r"
                % (meta_name, m.group("name"))
            )
        if meta_version != m.group("version"):
            raise WheelError(
                "METADATA Version %r conflicts with filename version %r"
                % (meta_version, m.group("version"))
            )

        info = WheelInfo(
            path=os.path.abspath(path),
            filename=filename,
            name=m.group("name"),
            version=m.group("version"),
            wheel_sha256=wheel_sha256,
            dist_info=dist_info,
        )

        # Hash every member (streamed, in memory; nothing is extracted).
        for n in names:
            if n.endswith("/"):
                continue
            with zf.open(n) as f:
                info.members[n] = _hash_stream(f)
            parts = n.split("/")
            if len(parts) > 1 and parts[0].endswith(".data"):
                info.data_members.append(n)

        # Parse and cross-check RECORD.
        raw = zf.read(record_name)
        try:
            text = raw.decode("utf-8")
            rows = list(csv.reader(io.StringIO(text)))
        except (UnicodeDecodeError, csv.Error) as e:
            raise WheelError("malformed RECORD: %s" % e)
        for row in rows:
            if not row:
                continue
            if len(row) > 3:
                raise WheelError("malformed RECORD row (too many fields): %r" % row[0])
            arc = row[0]
            digest_field = row[1] if len(row) > 1 else ""
            if not digest_field:
                info.record_entries_without_digest.append(arc)
                continue
            algo, _, b64 = digest_field.partition("=")
            if algo != "sha256" or not b64:
                raise WheelError(
                    "unsupported RECORD digest for %s: %r" % (arc, digest_field)
                )
            info.record_digests[arc] = b64
            observed = info.members.get(arc)
            if observed is None:
                raise WheelError("RECORD lists member not in archive: %s" % arc)
            if _b64_sha256(bytes.fromhex(observed)) != b64:
                raise WheelError("RECORD digest mismatch for %s" % arc)

        unlisted = sorted(set(info.members) - set(info.record_digests)
                          - set(info.record_entries_without_digest))
        if unlisted:
            info.warnings.append(
                "members missing from RECORD: %s" % ", ".join(unlisted[:5])
            )

        if info.data_members:
            info.warnings.append(
                "wheel uses .data relocation (%d members); mapping these to "
                "installed locations is unsupported" % len(info.data_members)
            )
        return info
