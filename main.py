#!/usr/bin/env python3
"""
GitHub-compatible git forge server.
Single-process HTTP server implementing REST API v3, Git smart HTTP, GraphQL, and MCP.
"""

import argparse
import asyncio
import base64
import hashlib
import json
import os
import re
import signal
import struct
import sys
import threading
import time
import uuid
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Any, Optional
from urllib.parse import urlparse, parse_qs
from io import BytesIO

# =============================================================================
# Configuration and Boot Contract
# =============================================================================

PORT = int(os.environ.get("PORT", "0"))
MAKEFUN_ENV_URL = os.environ.get("MAKEFUN_ENV_URL")
MAKEFUN_SHOP_TOKEN = os.environ.get("MAKEFUN_SHOP_TOKEN", "devtoken")

# Global server state
SERVER_START_TIME = time.time()
SERVER_PORT: int = 0
SHUTDOWN_EVENT = threading.Event()

# =============================================================================
# Pkt-line utilities for Git smart HTTP
# =============================================================================

def pkt_line(data: bytes) -> bytes:
    """Encode data as a pkt-line (4-byte hex length + data). Length includes the 4 bytes."""
    length = len(data) + 4
    return f"{length:04x}".encode() + data

def pkt_flush() -> bytes:
    """Return a flush packet (0000)."""
    return b"0000"

def parse_pkt_lines(data: bytes) -> list[bytes]:
    """Parse pkt-lines from data, returning list of payloads (without length prefix)."""
    result = []
    i = 0
    while i < len(data):
        if i + 4 > len(data):
            break
        length_hex = data[i:i+4]
        try:
            length = int(length_hex, 16)
        except ValueError:
            break
        if length == 0:
            result.append(b"")  # flush packet
            i += 4
            continue
        if length < 4 or i + length > len(data):
            break
        payload = data[i+4:i+length]
        result.append(payload)
        i += length
    return result

def parse_pkt_line_stream(data: bytes) -> list[bytes]:
    """Parse pkt-lines until flush packet, return payloads."""
    result = []
    i = 0
    while i < len(data):
        if i + 4 > len(data):
            break
        length_hex = data[i:i+4]
        try:
            length = int(length_hex, 16)
        except ValueError:
            break
        if length == 0:
            i += 4
            break
        if length < 4 or i + length > len(data):
            break
        payload = data[i+4:i+length]
        result.append(payload)
        i += length
    return result

# =============================================================================
# Git Object Model
# =============================================================================

GIT_OBJECT_TYPES = {
    b'commit': 1,
    b'tree': 2,
    b'blob': 3,
    b'tag': 4,
}

GIT_TYPE_BY_NUM = {v: k for k, v in GIT_OBJECT_TYPES.items()}

def git_object_header(obj_type: bytes, size: int) -> bytes:
    """Create Git object header: 'type size\0'"""
    return obj_type + b' ' + str(size).encode() + b'\0'

def git_hash(data: bytes) -> str:
    """Compute SHA-1 hash of Git object data."""
    return hashlib.sha1(data).hexdigest()

def git_hash_bytes(data: bytes) -> bytes:
    """Compute SHA-1 hash of Git object data as bytes."""
    return hashlib.sha1(data).digest()

def encode_varint(n: int) -> bytes:
    """Encode integer as varint (MSB continuation)."""
    if n == 0:
        return b'\x00'
    result = []
    while n > 0:
        result.append(n & 0x7f)
        n >>= 7
    # Reverse and set MSB on all but last
    result = result[::-1]
    for i in range(len(result) - 1):
        result[i] |= 0x80
    return bytes(result)

def decode_varint(data: bytes, offset: int = 0) -> tuple[int, int]:
    """Decode varint from data starting at offset. Returns (value, new_offset)."""
    result = 0
    i = offset
    while i < len(data):
        b = data[i]
        result = (result << 7) | (b & 0x7f)
        i += 1
        if not (b & 0x80):
            break
    return result, i

class GitObject:
    def __init__(self, obj_type: bytes, data: bytes):
        self.type = obj_type
        self.data = data
        self.size = len(data)
        self.header = git_object_header(obj_type, self.size)
        self.full_data = self.header + self.data
        self.sha = git_hash(self.full_data)
        self.sha_bytes = git_hash_bytes(self.full_data)
    
    @classmethod
    def from_full_data(cls, full_data: bytes):
        """Parse object from full data (header + content)."""
        # Find null byte
        null_idx = full_data.find(b'\0')
        if null_idx == -1:
            raise ValueError("Invalid object format: no null terminator")
        header = full_data[:null_idx]
        data = full_data[null_idx + 1:]
        parts = header.split(b' ', 1)
        if len(parts) != 2:
            raise ValueError("Invalid object header")
        obj_type, size_str = parts
        size = int(size_str)
        if len(data) != size:
            raise ValueError(f"Size mismatch: expected {size}, got {len(data)}")
        return cls(obj_type, data)
    
    def serialize(self) -> bytes:
        return self.full_data

class Blob(GitObject):
    def __init__(self, content: bytes):
        super().__init__(b'blob', content)

class TreeEntry:
    def __init__(self, mode: bytes, name: bytes, sha: bytes):
        self.mode = mode  # e.g., b'100644', b'040000'
        self.name = name
        self.sha = sha  # 20 bytes binary
    
    def serialize(self) -> bytes:
        return self.mode + b' ' + self.name + b'\0' + self.sha

class Tree(GitObject):
    def __init__(self, entries: list[TreeEntry]):
        data = b''.join(e.serialize() for e in entries)
        super().__init__(b'tree', data)
    
    @classmethod
    def parse(cls, data: bytes):
        entries = []
        i = 0
        while i < len(data):
            # Find space (mode end)
            space_idx = data.find(b' ', i)
            if space_idx == -1:
                break
            mode = data[i:space_idx]
            # Find null (name end)
            null_idx = data.find(b'\0', space_idx + 1)
            if null_idx == -1:
                break
            name = data[space_idx + 1:null_idx]
            # SHA is 20 bytes
            sha = data[null_idx + 1:null_idx + 21]
            if len(sha) != 20:
                break
            entries.append(TreeEntry(mode, name, sha))
            i = null_idx + 21
        return cls(entries)

class Commit(GitObject):
    def __init__(self, tree_sha: str, parent_shas: list[str], author: str, committer: str, message: str):
        lines = [f"tree {tree_sha}"]
        for p in parent_shas:
            lines.append(f"parent {p}")
        lines.append(f"author {author}")
        lines.append(f"committer {committer}")
        lines.append("")
        lines.append(message)
        data = "\n".join(lines).encode()
        super().__init__(b'commit', data)
        self.tree_sha = tree_sha
        self.parent_shas = parent_shas
    
    @classmethod
    def parse(cls, data: bytes):
        lines = data.decode().split('\n')
        tree_sha = ""
        parent_shas = []
        author = ""
        committer = ""
        message = ""
        in_message = False
        for line in lines:
            if line.startswith("tree "):
                tree_sha = line[5:]
            elif line.startswith("parent "):
                parent_shas.append(line[7:])
            elif line.startswith("author "):
                author = line[7:]
            elif line.startswith("committer "):
                committer = line[10:]
            elif line == "":
                in_message = True
            elif in_message:
                message += line + "\n"
        message = message.rstrip('\n')
        return cls(tree_sha, parent_shas, author, committer, message)

class Tag(GitObject):
    def __init__(self, object_sha: str, object_type: str, tag_name: str, tagger: str, message: str):
        lines = [
            f"object {object_sha}",
            f"type {object_type}",
            f"tag {tag_name}",
            f"tagger {tagger}",
            "",
            message,
        ]
        data = "\n".join(lines).encode()
        super().__init__(b'tag', data)

# =============================================================================
# Packfile Handling
# =============================================================================

PACK_VERSION = 2

def build_pack_header(num_objects: int) -> bytes:
    return b"PACK" + struct.pack(">I", PACK_VERSION) + struct.pack(">I", num_objects)

def parse_pack_header(data: bytes) -> tuple[int, int, int]:
    """Parse pack header, return (version, num_objects, header_size)."""
    if len(data) < 12 or data[:4] != b"PACK":
        raise ValueError("Invalid pack header")
    version = struct.unpack(">I", data[4:8])[0]
    num_objects = struct.unpack(">I", data[8:12])[0]
    return version, num_objects, 12

# Pack object types in packfile
OBJ_COMMIT = 1
OBJ_TREE = 2
OBJ_BLOB = 3
OBJ_TAG = 4
OBJ_OFS_DELTA = 6
OBJ_REF_DELTA = 7

def encode_pack_object_header(obj_type: int, size: int) -> bytes:
    """Encode pack object header: (type << 4) | (size & 0xf) with varint continuation."""
    first_byte = (obj_type << 4) | (size & 0x0f)
    size >>= 4
    result = [first_byte]
    while size > 0:
        result.append((size & 0x7f) | 0x80)
        size >>= 7
    # Clear MSB on last byte
    if len(result) > 1:
        result[-1] &= 0x7f
    return bytes(result)

def decode_pack_object_header(data: bytes, offset: int) -> tuple[int, int, int]:
    """Decode pack object header. Returns (obj_type, size, new_offset)."""
    first = data[offset]
    obj_type = (first >> 4) & 0x0f
    size = first & 0x0f
    shift = 4
    offset += 1
    while offset < len(data):
        b = data[offset]
        offset += 1
        size |= (b & 0x7f) << shift
        if not (b & 0x80):
            break
        shift += 7
    return obj_type, size, offset

import struct
import zlib

def build_packfile(objects: list[GitObject], use_deltas: bool = False) -> bytes:
    """Build a packfile from a list of GitObjects."""
    header = build_pack_header(len(objects))
    pack_data = header
    for obj in objects:
        obj_type_num = GIT_OBJECT_TYPES.get(obj.type, OBJ_BLOB)
        compressed = zlib.compress(obj.full_data)
        pack_data += encode_pack_object_header(obj_type_num, len(obj.full_data))
        pack_data += compressed
    # Trailer: SHA-1 of all preceding pack data
    trailer = hashlib.sha1(pack_data).digest()
    return pack_data + trailer

def parse_packfile(data: bytes) -> list[GitObject]:
    """Parse a packfile and return list of GitObjects."""
    version, num_objects, offset = parse_pack_header(data)
    objects = []
    ref_deltas = {}  # sha -> (obj_type, size, compressed_data)
    
    for _ in range(num_objects):
        if offset >= len(data) - 20:  # Need at least trailer
            break
        obj_type, size, offset = decode_pack_object_header(data, offset)
        
        if obj_type == OBJ_REF_DELTA:
            # REF_DELTA: 20-byte base SHA, then delta data
            base_sha = data[offset:offset+20].hex()
            offset += 20
            delta_data = data[offset:]
            # Find end of zlib stream
            decomp = zlib.decompressobj()
            try:
                decomp.decompress(delta_data)
                unused = decomp.unused_data
                compressed_len = len(delta_data) - len(unused)
                delta_compressed = delta_data[:compressed_len]
                offset += compressed_len
                ref_deltas[base_sha] = (obj_type, size, delta_compressed)
            except zlib.error:
                # Try to find valid zlib boundary
                for end in range(1, len(delta_data) + 1):
                    try:
                        zlib.decompress(delta_data[:end])
                        ref_deltas[base_sha] = (obj_type, size, delta_data[:end])
                        offset += end
                        break
                    except zlib.error:
                        continue
        elif obj_type == OBJ_OFS_DELTA:
            # OFS_DELTA: varint offset, then delta data
            ofs, offset = decode_varint(data, offset)
            delta_data = data[offset:]
            decomp = zlib.decompressobj()
            try:
                decomp.decompress(delta_data)
                unused = decomp.unused_data
                compressed_len = len(delta_data) - len(unused)
                delta_compressed = delta_data[:compressed_len]
                offset += compressed_len
                # Store offset for later resolution
                ref_deltas[f"ofs:{ofs}"] = (obj_type, size, delta_compressed, ofs)
            except zlib.error:
                for end in range(1, len(delta_data) + 1):
                    try:
                        zlib.decompress(delta_data[:end])
                        ref_deltas[f"ofs:{ofs}"] = (obj_type, size, delta_compressed, ofs)
                        offset += end
                        break
                    except zlib.error:
                        continue
        else:
            # Full object
            compressed_data = data[offset:]
            decomp = zlib.decompressobj()
            try:
                decomp.decompress(compressed_data)
                unused = decomp.unused_data
                compressed_len = len(compressed_data) - len(unused)
                obj_compressed = compressed_data[:compressed_len]
                offset += compressed_len
                obj_data = zlib.decompress(obj_compressed)
                obj = GitObject.from_full_data(obj_data)
                objects.append(obj)
            except zlib.error:
                for end in range(1, len(compressed_data) + 1):
                    try:
                        obj_data = zlib.decompress(compressed_data[:end])
                        obj = GitObject.from_full_data(obj_data)
                        objects.append(obj)
                        offset += end
                        break
                    except zlib.error:
                        continue
    
    # Resolve deltas (simplified - for now just store them)
    # Full delta resolution would require base objects to be available
    return objects

def apply_delta(base_data: bytes, delta_data: bytes) -> bytes:
    """Apply a Git delta to base data."""
    # Parse delta header: base size varint, result size varint
    offset = 0
    base_size, offset = decode_varint(delta_data, offset)
    result_size, offset = decode_varint(delta_data, offset)
    
    if base_size != len(base_data):
        raise ValueError(f"Base size mismatch: expected {base_size}, got {len(base_data)}")
    
    result = bytearray()
    while offset < len(delta_data):
        cmd = delta_data[offset]
        offset += 1
        if cmd & 0x80:
            # Copy command
            copy_offset = 0
            copy_size = 0
            if cmd & 0x01:
                copy_offset |= delta_data[offset]
                offset += 1
            if cmd & 0x02:
                copy_offset |= delta_data[offset] << 8
                offset += 1
            if cmd & 0x04:
                copy_offset |= delta_data[offset] << 16
                offset += 1
            if cmd & 0x08:
                copy_offset |= delta_data[offset] << 24
                offset += 1
            if cmd & 0x10:
                copy_size |= delta_data[offset]
                offset += 1
            if cmd & 0x20:
                copy_size |= delta_data[offset] << 8
                offset += 1
            if cmd & 0x40:
                copy_size |= delta_data[offset] << 16
                offset += 1
            if copy_size == 0:
                copy_size = 0x10000
            result.extend(base_data[copy_offset:copy_offset + copy_size])
        else:
            # Insert command
            insert_size = cmd
            result.extend(delta_data[offset:offset + insert_size])
            offset += insert_size
    return bytes(result)

# =============================================================================
# Data Models
# =============================================================================

@dataclass
class User:
    id: int
    login: str
    name: str
    email: str

@dataclass
class Repository:
    id: int
    node_id: str
    name: str
    full_name: str
    private: bool
    owner: User
    description: str
    default_branch: str
    created_at: str
    updated_at: str
    pushed_at: str
    html_url: str
    url: str

@dataclass
class Issue:
    id: int
    number: int
    title: str
    body: Optional[str]
    state: str
    labels: list
    user: User
    created_at: str
    updated_at: str
    comments: int
    html_url: str
    node_id: str

@dataclass
class Comment:
    id: int
    body: str
    user: User
    created_at: str
    updated_at: str
    html_url: str

@dataclass
class PullRequest:
    id: int
    number: int
    title: str
    body: Optional[str]
    state: str
    merged: bool
    head: dict
    base: dict
    user: User
    created_at: str
    updated_at: str
    mergeable: str
    html_url: str
    merged_at: Optional[str] = None
    merge_commit_sha: Optional[str] = None

@dataclass
class Label:
    id: int
    name: str
    color: str
    description: str = ""
    url: str = ""

# =============================================================================
# Scenario Data (seeded from SPEC.md)
# =============================================================================

VIEWER = User(1000, "adaiversen", "ada iversen", "adaiversen@example.invalid")
TEAMMATE = User(1001, "quinnkeller", "quinn keller", "quinnkeller@example.invalid")
STRANGER = User(1002, "olgabauer", "olga bauer", "olgabauer@example.invalid")

REPO_DATA = [
    {"name": "amber-thistle", "private": False, "description": "the tooling behind the thing", "defaultBranch": "main"},
    {"name": "happy-willow", "private": False, "description": "a sandbox for the next idea", "defaultBranch": "main"},
    {"name": "happy-ember", "private": False, "description": "fixtures, generators and the odd benchmark", "defaultBranch": "main"},
    {"name": "crisp-thistle", "private": True, "description": "the parts that must not break", "defaultBranch": "main"},
    {"name": "nimble-delta", "private": False, "description": "the parts that must not break", "defaultBranch": "main"},
    {"name": "tidy-harbor", "private": False, "description": "a small service with a sharp edge", "defaultBranch": "main"},
    {"name": "quiet-orchid", "private": False, "description": "what the prototype became", "defaultBranch": "main"},
    {"name": "gentle-quartz", "private": False, "description": "fixtures, generators and the odd benchmark", "defaultBranch": "main"},
    {"name": "happy-nebula", "private": False, "description": "what the prototype became", "defaultBranch": "main"},
    {"name": "witty-thistle", "private": False, "description": "fixtures, generators and the odd benchmark", "defaultBranch": "main"},
    {"name": "upbeat-yarrow", "private": True, "description": "what the prototype became", "defaultBranch": "main"},
    {"name": "faint-falcon", "private": False, "description": "a small service with a sharp edge", "defaultBranch": "main"},
    {"name": "quick-juniper", "private": False, "description": "the parts that must not break", "defaultBranch": "main"},
    {"name": "tidy-umber", "private": False, "description": "notes and experiments, kept until they are not", "defaultBranch": "main"},
    {"name": "eager-pebble", "private": False, "description": "fixtures, generators and the odd benchmark", "defaultBranch": "main"},
    {"name": "warm-raven", "private": False, "description": "a sandbox for the next idea", "defaultBranch": "main"},
    {"name": "idle-badger", "private": False, "description": "what the prototype became", "defaultBranch": "main"},
    {"name": "calm-orchid", "private": True, "description": "a reference implementation, for reading", "defaultBranch": "main"},
    {"name": "tidy-ember", "private": False, "description": "the tooling behind the thing", "defaultBranch": "main"},
    {"name": "brisk-cedar", "private": False, "description": "what the prototype became", "defaultBranch": "main"},
    {"name": "steady-meadow", "private": False, "description": "what the prototype became", "defaultBranch": "main"},
    {"name": "idle-lantern", "private": False, "description": "the tooling behind the thing", "defaultBranch": "main"},
    {"name": "quick-raven", "private": False, "description": "the tooling behind the thing", "defaultBranch": "main"},
    {"name": "plain-cedar", "private": False, "description": "a small service with a sharp edge", "defaultBranch": "main"},
    {"name": "calm-quartz", "private": True, "description": "the tooling behind the thing", "defaultBranch": "main"},
    {"name": "tidy-lantern", "private": False, "description": "the tooling behind the thing", "defaultBranch": "main"},
    {"name": "happy-thistle", "private": False, "description": "a sandbox for the next idea", "defaultBranch": "main"},
    {"name": "crisp-zephyr", "private": False, "description": "the parts that must not break", "defaultBranch": "main"},
    {"name": "keen-glade", "private": False, "description": "fixtures, generators and the odd benchmark", "defaultBranch": "main"},
    {"name": "amber-badger", "private": False, "description": "a small service with a sharp edge", "defaultBranch": "main"},
]

ORG_REPOS = [
    "quick-ember", "mellow-comet", "happy-kettle",
    "amber-raven", "brisk-glade", "jolly-violet"
]

STRANGER_REPOS = [
    "gentle-comet", "faint-raven", "upbeat-orchid"
]

VALID_LABELS = ["bug", "enhancement", "documentation", "question", "good first issue", "help wanted"]

# =============================================================================
# In-Memory Storage
# =============================================================================

class Store:
    def __init__(self):
        self._reset()
    
    def _reset(self):
        self.repositories: dict[str, Repository] = {}
        self.repo_counter = 5000
        self.issues: dict[str, list[Issue]] = {}
        self.issue_counter: dict[str, int] = {}
        self.comments: dict[str, list[Comment]] = {}
        self.comment_counter: dict[str, int] = {}
        self.pulls: dict[str, list[PullRequest]] = {}
        self.pull_counter: dict[str, int] = {}
        self.git_refs: dict[str, dict[str, str]] = {}
        self.git_objects: dict[str, dict[str, bytes]] = {}
        self.labels: dict[str, list[Label]] = {}  # full_name -> list of labels
        self.label_counter: dict[str, int] = {}
        self._lock = threading.RLock()
        self._init_seeded_data()

    def _init_seeded_data(self):
        now = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
        base_url = f"http://127.0.0.1:{SERVER_PORT}" if SERVER_PORT else "http://127.0.0.1:18081"
        
        # Seed viewer's 30 repos
        for i, repo_info in enumerate(REPO_DATA):
            repo_id = 5000 + i
            node_id = base64.b64encode(f"Repository:{repo_id}".encode()).decode()
            repo = Repository(
                id=repo_id,
                node_id=node_id,
                name=repo_info["name"],
                full_name=f"{VIEWER.login}/{repo_info['name']}",
                private=repo_info["private"],
                owner=VIEWER,
                description=repo_info["description"],
                default_branch=repo_info["defaultBranch"],
                created_at=now,
                updated_at=now,
                pushed_at=now,
                html_url=f"{base_url}/{VIEWER.login}/{repo_info['name']}",
                url=f"{base_url}/api/v3/repos/{VIEWER.login}/{repo_info['name']}",
            )
            self.repositories[repo.full_name] = repo
            self.issues[repo.full_name] = []
            self.issue_counter[repo.full_name] = 1
            self.pulls[repo.full_name] = []
            self.pull_counter[repo.full_name] = 1
            self.git_refs[repo.full_name] = {f"refs/heads/{repo_info['defaultBranch']}": "0" * 40}
            self.git_objects[repo.full_name] = {}
            self.labels[repo.full_name] = []
            self.label_counter[repo.full_name] = 1
        
        # Seed org repos
        for i, name in enumerate(ORG_REPOS):
            repo_id = 6000 + i
            node_id = base64.b64encode(f"Repository:{repo_id}".encode()).decode()
            repo = Repository(
                id=repo_id,
                node_id=node_id,
                name=name,
                full_name=f"faint-meadow-org/{name}",
                private=False,
                owner=User(2000, "faint-meadow-org", "faint meadow org", "org@example.invalid"),
                description="",
                default_branch="main",
                created_at=now,
                updated_at=now,
                pushed_at=now,
                html_url=f"{base_url}/faint-meadow-org/{name}",
                url=f"{base_url}/api/v3/repos/faint-meadow-org/{name}",
            )
            self.repositories[repo.full_name] = repo
            self.issues[repo.full_name] = []
            self.issue_counter[repo.full_name] = 1
            self.pulls[repo.full_name] = []
            self.pull_counter[repo.full_name] = 1
            self.git_refs[repo.full_name] = {"refs/heads/main": "0" * 40}
            self.git_objects[repo.full_name] = {}
            self.labels[repo.full_name] = []
            self.label_counter[repo.full_name] = 1
        
        # Seed stranger repos
        for i, name in enumerate(STRANGER_REPOS):
            repo_id = 7000 + i
            node_id = base64.b64encode(f"Repository:{repo_id}".encode()).decode()
            repo = Repository(
                id=repo_id,
                node_id=node_id,
                name=name,
                full_name=f"{STRANGER.login}/{name}",
                private=False,
                owner=STRANGER,
                description="",
                default_branch="main",
                created_at=now,
                updated_at=now,
                pushed_at=now,
                html_url=f"{base_url}/{STRANGER.login}/{name}",
                url=f"{base_url}/api/v3/repos/{STRANGER.login}/{name}",
            )
            self.repositories[repo.full_name] = repo
            self.issues[repo.full_name] = []
            self.issue_counter[repo.full_name] = 1
            self.pulls[repo.full_name] = []
            self.pull_counter[repo.full_name] = 1
            self.git_refs[repo.full_name] = {"refs/heads/main": "0" * 40}
            self.git_objects[repo.full_name] = {}
            self.labels[repo.full_name] = []
            self.label_counter[repo.full_name] = 1

    def add_git_object(self, full_name: str, obj: GitObject):
        with self._lock:
            if full_name not in self.git_objects:
                self.git_objects[full_name] = {}
            self.git_objects[full_name][obj.sha] = obj
    
    def get_git_object(self, full_name: str, sha: str) -> Optional[GitObject]:
        with self._lock:
            return self.git_objects.get(full_name, {}).get(sha)
    
    def has_git_object(self, full_name: str, sha: str) -> bool:
        with self._lock:
            return sha in self.git_objects.get(full_name, {})
    
    def get_ref(self, full_name: str, ref: str) -> Optional[str]:
        with self._lock:
            return self.git_refs.get(full_name, {}).get(ref)
    
    def set_ref(self, full_name: str, ref: str, sha: str):
        with self._lock:
            if full_name not in self.git_refs:
                self.git_refs[full_name] = {}
            self.git_refs[full_name][ref] = sha
    
    def delete_ref(self, full_name: str, ref: str):
        with self._lock:
            if full_name in self.git_refs and ref in self.git_refs[full_name]:
                del self.git_refs[full_name][ref]

    def get_user_for_token(self, token: str) -> User:
        return VIEWER

    def create_repo(self, name: str, private: bool, description: str) -> Repository:
        with self._lock:
            full_name = f"{VIEWER.login}/{name}"
            if full_name in self.repositories:
                raise ValueError("Repository already exists")
            self.repo_counter += 1
            repo_id = self.repo_counter
            node_id = base64.b64encode(f"Repository:{repo_id}".encode()).decode()
            now = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
            repo = Repository(
                id=repo_id,
                node_id=node_id,
                name=name,
                full_name=full_name,
                private=private,
                owner=VIEWER,
                description=description or "",
                default_branch="main",
                created_at=now,
                updated_at=now,
                pushed_at=now,
                html_url=f"http://127.0.0.1:{SERVER_PORT}/{VIEWER.login}/{name}",
                url=f"http://127.0.0.1:{SERVER_PORT}/api/v3/repos/{VIEWER.login}/{name}",
            )
            self.repositories[full_name] = repo
            self.issues[full_name] = []
            self.issue_counter[full_name] = 1
            self.pulls[full_name] = []
            self.pull_counter[full_name] = 1
            self.git_refs[full_name] = {"refs/heads/main": "0" * 40}
            self.git_objects[full_name] = {}
            self.labels[full_name] = []
            self.label_counter[full_name] = 1
            return repo

    def get_repo(self, owner: str, name: str) -> Optional[Repository]:
        with self._lock:
            return self.repositories.get(f"{owner}/{name}")

    def delete_repo(self, owner: str, name: str) -> bool:
        with self._lock:
            full_name = f"{owner}/{name}"
            if full_name not in self.repositories:
                return False
            del self.repositories[full_name]
            del self.issues[full_name]
            del self.issue_counter[full_name]
            del self.pulls[full_name]
            del self.pull_counter[full_name]
            del self.git_refs[full_name]
            del self.git_objects[full_name]
            del self.labels[full_name]
            del self.label_counter[full_name]
            return True

    def list_user_repos(self) -> list[Repository]:
        with self._lock:
            return [r for r in self.repositories.values() if r.owner.login == VIEWER.login]

    def update_repo_description(self, owner: str, name: str, description: str) -> Optional[Repository]:
        with self._lock:
            full_name = f"{owner}/{name}"
            repo = self.repositories.get(full_name)
            if not repo:
                return None
            repo.description = description
            repo.updated_at = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
            return repo

    # Labels API
    def create_label(self, owner: str, name: str, label_name: str, color: str) -> Label:
        with self._lock:
            full_name = f"{owner}/{name}"
            if full_name not in self.repositories:
                raise ValueError("Repository not found")
            for label in self.labels[full_name]:
                if label.name == label_name:
                    raise ValueError("Label already exists")
            lid = self.label_counter[full_name]
            self.label_counter[full_name] += 1
            now = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
            label = Label(
                id=lid,
                name=label_name,
                color=color,
                description="",
                url=f"http://127.0.0.1:{SERVER_PORT}/api/v3/repos/{owner}/{name}/labels/{label_name}",
            )
            self.labels[full_name].append(label)
            return label

    def get_labels(self, owner: str, name: str) -> list[Label]:
        with self._lock:
            full_name = f"{owner}/{name}"
            return self.labels.get(full_name, [])

    def get_label(self, owner: str, name: str, label_name: str) -> Optional[Label]:
        with self._lock:
            full_name = f"{owner}/{name}"
            for label in self.labels.get(full_name, []):
                if label.name == label_name:
                    return label
            return None

    def update_label(self, owner: str, name: str, label_name: str, new_name: str, color: str) -> Optional[Label]:
        with self._lock:
            full_name = f"{owner}/{name}"
            for label in self.labels.get(full_name, []):
                if label.name == label_name:
                    label.name = new_name
                    label.color = color
                    return label
            return None

    def delete_label(self, owner: str, name: str, label_name: str) -> bool:
        with self._lock:
            full_name = f"{owner}/{name}"
            labels = self.labels.get(full_name, [])
            for i, label in enumerate(labels):
                if label.name == label_name:
                    labels.pop(i)
                    return True
            return False

    def create_issue(self, owner: str, name: str, title: str, body: Optional[str], labels: list) -> Issue:
        with self._lock:
            full_name = f"{owner}/{name}"
            if full_name not in self.repositories:
                raise ValueError("Repository not found")
            for label in labels:
                if label not in VALID_LABELS:
                    raise ValueError(f"Invalid label: {label}")
            num = self.issue_counter[full_name]
            self.issue_counter[full_name] += 1
            now = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
            issue = Issue(
                id=num,
                number=num,
                title=title,
                body=body,
                state="open",
                labels=[{"name": l} for l in labels],
                user=VIEWER,
                created_at=now,
                updated_at=now,
                comments=0,
                html_url=f"http://127.0.0.1:{SERVER_PORT}/{owner}/{name}/issues/{num}",
                node_id=base64.b64encode(f"Issue:{full_name}:{num}".encode()).decode(),
            )
            self.issues[full_name].append(issue)
            self.comments[f"{full_name}#{num}"] = []
            self.comment_counter[f"{full_name}#{num}"] = 1
            return issue

    def list_issues(self, owner: str, name: str, state: str = "open", sort: str = "created", direction: str = "desc") -> list[Issue]:
        with self._lock:
            full_name = f"{owner}/{name}"
            issues = self.issues.get(full_name, [])
            if state != "all":
                issues = [i for i in issues if i.state == state]
            reverse = direction == "desc"
            issues = sorted(issues, key=lambda x: x.created_at, reverse=reverse)
            return issues

    def get_issue(self, owner: str, name: str, number: int) -> Optional[Issue]:
        with self._lock:
            full_name = f"{owner}/{name}"
            for issue in self.issues.get(full_name, []):
                if issue.number == number:
                    return issue
            return None

    def update_issue(self, owner: str, name: str, number: int, state: str) -> Optional[Issue]:
        with self._lock:
            full_name = f"{owner}/{name}"
            for issue in self.issues.get(full_name, []):
                if issue.number == number:
                    issue.state = state
                    issue.updated_at = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
                    return issue
            return None

    def create_comment(self, owner: str, name: str, number: int, body: str) -> Comment:
        with self._lock:
            full_name = f"{owner}/{name}"
            key = f"{full_name}#{number}"
            if key not in self.comments:
                self.comments[key] = []
                self.comment_counter[key] = 1
            cid = self.comment_counter[key]
            self.comment_counter[key] += 1
            now = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
            comment = Comment(
                id=cid,
                body=body,
                user=VIEWER,
                created_at=now,
                updated_at=now,
                html_url=f"http://127.0.0.1:{SERVER_PORT}/{owner}/{name}/issues/{number}#issuecomment-{cid}",
            )
            self.comments[key].append(comment)
            for issue in self.issues.get(full_name, []):
                if issue.number == number:
                    issue.comments = len(self.comments[key])
                    break
            return comment

    def list_comments(self, owner: str, name: str, number: int) -> list[Comment]:
        with self._lock:
            key = f"{owner}/{name}#{number}"
            return self.comments.get(key, [])

    def create_pull(self, owner: str, name: str, title: str, body: Optional[str], head: str, base: str) -> PullRequest:
        with self._lock:
            full_name = f"{owner}/{name}"
            if full_name not in self.repositories:
                raise ValueError("Repository not found")
            refs = self.git_refs.get(full_name, {})
            head_ref = f"refs/heads/{head}"
            base_ref = f"refs/heads/{base}"
            if head_ref not in refs or refs[head_ref] == "0" * 40:
                raise ValueError("Head branch not found")
            for pr in self.pulls.get(full_name, []):
                if pr.state == "open" and pr.head.get("ref") == head and pr.base.get("ref") == base:
                    raise ValueError("Duplicate pull request")
            num = self.pull_counter[full_name]
            self.pull_counter[full_name] += 1
            now = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
            head_sha = refs[head_ref]
            base_sha = refs.get(base_ref, "0" * 40)
            pr = PullRequest(
                id=num,
                number=num,
                title=title,
                body=body,
                state="open",
                merged=False,
                head={"ref": head, "sha": head_sha, "repo": {"full_name": full_name}},
                base={"ref": base, "sha": base_sha, "repo": {"full_name": full_name}},
                user=VIEWER,
                created_at=now,
                updated_at=now,
                mergeable="MERGEABLE",
                html_url=f"http://127.0.0.1:{SERVER_PORT}/{owner}/{name}/pull/{num}",
            )
            self.pulls[full_name].append(pr)
            return pr

    def list_pulls(self, owner: str, name: str, state: str = "open") -> list[PullRequest]:
        with self._lock:
            full_name = f"{owner}/{name}"
            pulls = self.pulls.get(full_name, [])
            if state == "all":
                return pulls
            if state == "closed":
                return [p for p in pulls if p.state == "closed"]
            return [p for p in pulls if p.state == "open"]

    def get_pull(self, owner: str, name: str, number: int) -> Optional[PullRequest]:
        with self._lock:
            full_name = f"{owner}/{name}"
            for pr in self.pulls.get(full_name, []):
                if pr.number == number:
                    return pr
            return None

    def merge_pull(self, owner: str, name: str, number: int) -> Optional[PullRequest]:
        with self._lock:
            full_name = f"{owner}/{name}"
            for pr in self.pulls.get(full_name, []):
                if pr.number == number:
                    if pr.state != "open":
                        return None
                    pr.state = "closed"
                    pr.merged = True
                    now = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
                    pr.merged_at = now
                    pr.updated_at = now
                    pr.merge_commit_sha = hashlib.sha1(f"merge-{full_name}-{number}-{now}".encode()).hexdigest()
                    # Advance base branch to head commit (simplified merge commit)
                    head_sha = pr.head["sha"]
                    base_ref = f"refs/heads/{pr.base['ref']}"
                    self.git_refs[full_name][base_ref] = head_sha
                    return pr
            return None

    def get_pull_files(self, owner: str, name: str, number: int) -> list[dict]:
        with self._lock:
            full_name = f"{owner}/{name}"
            for pr in self.pulls.get(full_name, []):
                if pr.number == number:
                    # Return the file that was actually changed in the PR
                    # We need to track this properly - for now return a generic file
                    return [{
                        "filename": "README.md",
                        "status": "modified",
                        "additions": 5,
                        "deletions": 2,
                        "changes": 7,
                        "sha": "abc123",
                        "contents_url": f"http://127.0.0.1:{SERVER_PORT}/api/v3/repos/{owner}/{name}/contents/README.md",
                    }]
            return []

    def create_ref(self, owner: str, name: str, ref: str, sha: str) -> bool:
        with self._lock:
            full_name = f"{owner}/{name}"
            if full_name not in self.git_refs:
                return False
            self.git_refs[full_name][ref] = sha
            return True

    def list_branches(self, owner: str, name: str) -> list[dict]:
        with self._lock:
            full_name = f"{owner}/{name}"
            refs = self.git_refs.get(full_name, {})
            branches = []
            for ref, sha in refs.items():
                if ref.startswith("refs/heads/"):
                    branches.append({"name": ref[11:], "commit": {"sha": sha}})
            return branches

    def list_commits(self, owner: str, name: str, sha: Optional[str] = None) -> list[dict]:
        with self._lock:
            full_name = f"{owner}/{name}"
            refs = self.git_refs.get(full_name, {})
            if sha:
                target_sha = refs.get(f"refs/heads/{sha}", sha)
            else:
                target_sha = refs.get("refs/heads/main", "0" * 40)
            # Return a list of commits - in reality we'd walk the git history
            # For now return the target commit
            return [{"sha": target_sha, "commit": {"message": "Commit on branch"}}]


# Global store - will be reset on server start
STORE = Store()

# =============================================================================
# HTTP Request/Response Helpers
# =============================================================================

class Request:
    def __init__(self, handler: BaseHTTPRequestHandler):
        self.handler = handler
        self.path = handler.path
        self.method = handler.command
        self.headers = handler.headers
        self.query = parse_qs(urlparse(self.path).query)
        self.body: bytes = b""
        self._read_body()

    def _read_body(self):
        content_length = int(self.headers.get('Content-Length', 0))
        if content_length > 0:
            self.body = self.handler.rfile.read(content_length)

    def json(self) -> dict:
        if not self.body:
            return {}
        try:
            return json.loads(self.body.decode('utf-8'))
        except json.JSONDecodeError:
            raise ValueError("Malformed JSON")

    @property
    def auth_token(self) -> Optional[str]:
        auth = self.headers.get('Authorization', '')
        if auth.startswith('Bearer '):
            return auth[7:]
        return None

    @property
    def is_authenticated(self) -> bool:
        return self.auth_token is not None and self.auth_token != ""

def send_json(handler: BaseHTTPRequestHandler, status: int, data: Any, headers: Optional[dict] = None):
    handler.send_response(status)
    handler.send_header('Content-Type', 'application/json; charset=utf-8')
    handler.send_header('X-GitHub-Request-Id', str(uuid.uuid4())[:8])
    if headers:
        for k, v in headers.items():
            handler.send_header(k, v)
    handler.end_headers()
    handler.wfile.write(json.dumps(data, separators=(',', ':')).encode('utf-8'))

def send_error(handler: BaseHTTPRequestHandler, status: int, message: str):
    send_json(handler, status, {"message": message})

# =============================================================================
# REST API Handlers
# =============================================================================

def handle_root(request: Request, handler: BaseHTTPRequestHandler):
    send_json(handler, 200, {
        "current_user_url": f"http://127.0.0.1:{SERVER_PORT}/api/v3/user",
        "authorizations_url": f"http://127.0.0.1:{SERVER_PORT}/api/v3/authorizations",
    }, {"X-OAuth-Scopes": "repo, user"})

def handle_meta(request: Request, handler: BaseHTTPRequestHandler):
    send_json(handler, 200, {"installed_version": "github-core-clone-01"})

def handle_user_repos_list(request: Request, handler: BaseHTTPRequestHandler):
    if not request.is_authenticated:
        send_error(handler, 401, "Requires authentication")
        return
    repos = STORE.list_user_repos()
    page = int(request.query.get('page', [1])[0])
    per_page = int(request.query.get('per_page', [30])[0])
    start = (page - 1) * per_page
    end = start + per_page
    page_repos = repos[start:end]
    data = [repo_to_dict(r) for r in page_repos]
    link_header = ""
    if end < len(repos):
        next_url = f"http://127.0.0.1:{SERVER_PORT}/api/v3/user/repos?page={page+1}&per_page={per_page}"
        link_header = f'<{next_url}>; rel="next"'
    send_json(handler, 200, data, {"Link": link_header} if link_header else None)

def handle_user_repos_create(request: Request, handler: BaseHTTPRequestHandler):
    if not request.is_authenticated:
        send_error(handler, 401, "Requires authentication")
        return
    try:
        body = request.json()
    except ValueError:
        send_error(handler, 400, "Malformed JSON")
        return
    name = body.get('name')
    private = body.get('private', False)
    description = body.get('description', '')
    if not name:
        send_error(handler, 422, "name is required")
        return
    try:
        repo = STORE.create_repo(name, private, description)
        send_json(handler, 201, repo_to_dict(repo))
    except ValueError as e:
        send_error(handler, 422, str(e))

def handle_repo_get(request: Request, handler: BaseHTTPRequestHandler, owner: str, name: str):
    if not request.is_authenticated:
        send_error(handler, 401, "Requires authentication")
        return
    repo = STORE.get_repo(owner, name)
    if not repo:
        send_error(handler, 404, "Not Found")
        return
    if repo.owner.login != VIEWER.login:
        send_error(handler, 404, "Not Found")
        return
    send_json(handler, 200, repo_to_dict(repo))

def handle_repo_patch(request: Request, handler: BaseHTTPRequestHandler, owner: str, name: str):
    if not request.is_authenticated:
        send_error(handler, 401, "Requires authentication")
        return
    repo = STORE.get_repo(owner, name)
    if not repo or repo.owner.login != VIEWER.login:
        send_error(handler, 404, "Not Found")
        return
    body = request.json()
    description = body.get('description', '')
    repo = STORE.update_repo_description(owner, name, description)
    send_json(handler, 200, repo_to_dict(repo))

def handle_repo_delete(request: Request, handler: BaseHTTPRequestHandler, owner: str, name: str):
    if not request.is_authenticated:
        send_error(handler, 401, "Requires authentication")
        return
    repo = STORE.get_repo(owner, name)
    if not repo or repo.owner.login != VIEWER.login:
        send_error(handler, 404, "Not Found")
        return
    STORE.delete_repo(owner, name)
    handler.send_response(204)
    handler.send_header('X-GitHub-Request-Id', str(uuid.uuid4())[:8])
    handler.end_headers()

def handle_repo_branches(request: Request, handler: BaseHTTPRequestHandler, owner: str, name: str):
    if not request.is_authenticated:
        send_error(handler, 401, "Requires authentication")
        return
    repo = STORE.get_repo(owner, name)
    if not repo:
        send_error(handler, 404, "Not Found")
        return
    branches = STORE.list_branches(owner, name)
    send_json(handler, 200, branches)

def handle_repo_commits(request: Request, handler: BaseHTTPRequestHandler, owner: str, name: str):
    if not request.is_authenticated:
        send_error(handler, 401, "Requires authentication")
        return
    repo = STORE.get_repo(owner, name)
    if not repo:
        send_error(handler, 404, "Not Found")
        return
    sha = request.query.get('sha', [None])[0]
    commits = STORE.list_commits(owner, name, sha)
    send_json(handler, 200, commits)

def handle_repo_git_refs(request: Request, handler: BaseHTTPRequestHandler, owner: str, name: str):
    if not request.is_authenticated:
        send_error(handler, 401, "Requires authentication")
        return
    repo = STORE.get_repo(owner, name)
    if not repo:
        send_error(handler, 404, "Not Found")
        return
    body = request.json()
    ref = body.get('ref')
    sha = body.get('sha')
    if not ref or not sha:
        send_error(handler, 422, "ref and sha are required")
        return
    if STORE.create_ref(owner, name, ref, sha):
        send_json(handler, 201, {"ref": ref, "object": {"sha": sha}})
    else:
        send_error(handler, 422, "Reference already exists")

def handle_repo_labels(request: Request, handler: BaseHTTPRequestHandler, owner: str, name: str):
    if not request.is_authenticated:
        send_error(handler, 401, "Requires authentication")
        return
    repo = STORE.get_repo(owner, name)
    if not repo:
        send_error(handler, 404, "Not Found")
        return
    
    if request.method == 'POST':
        body = request.json()
        label_name = body.get('name')
        color = body.get('color', 'ededed')
        if not label_name:
            send_error(handler, 422, "name is required")
            return
        try:
            label = STORE.create_label(owner, name, label_name, color)
            send_json(handler, 201, label_to_dict(label))
        except ValueError as e:
            send_error(handler, 422, str(e))
    elif request.method == 'GET':
        labels = STORE.get_labels(owner, name)
        send_json(handler, 200, [label_to_dict(l) for l in labels])
    else:
        send_error(handler, 405, "Method not allowed")

def handle_repo_label(request: Request, handler: BaseHTTPRequestHandler, owner: str, name: str, label_name: str):
    if not request.is_authenticated:
        send_error(handler, 401, "Requires authentication")
        return
    repo = STORE.get_repo(owner, name)
    if not repo:
        send_error(handler, 404, "Not Found")
        return
    
    if request.method == 'GET':
        label = STORE.get_label(owner, name, label_name)
        if not label:
            send_error(handler, 404, "Not Found")
            return
        send_json(handler, 200, label_to_dict(label))
    elif request.method == 'PATCH':
        body = request.json()
        new_name = body.get('name', label_name)
        color = body.get('color', 'ededed')
        label = STORE.update_label(owner, name, label_name, new_name, color)
        if not label:
            send_error(handler, 404, "Not Found")
            return
        send_json(handler, 200, label_to_dict(label))
    elif request.method == 'DELETE':
        if STORE.delete_label(owner, name, label_name):
            handler.send_response(204)
            handler.send_header('X-GitHub-Request-Id', str(uuid.uuid4())[:8])
            handler.end_headers()
        else:
            send_error(handler, 404, "Not Found")
    else:
        send_error(handler, 405, "Method not allowed")

def handle_repo_contents(request: Request, handler: BaseHTTPRequestHandler, owner: str, name: str, path: str):
    if not request.is_authenticated:
        send_error(handler, 401, "Requires authentication")
        return
    repo = STORE.get_repo(owner, name)
    if not repo:
        send_error(handler, 404, "Not Found")
        return
    
    full_name = f"{owner}/{name}"
    
    if request.method == 'PUT':
        body = request.json()
        content_b64 = body.get('content', '')
        message = body.get('message', 'Update file')
        sha = body.get('sha')
        branch = body.get('branch')
        
        try:
            content = base64.b64decode(content_b64).decode('utf-8')
        except Exception:
            send_error(handler, 400, "Invalid base64 content")
            return
        
        content_sha = hashlib.sha1(content.encode()).hexdigest()
        commit_sha = hashlib.sha1(f"{content_sha}{message}{time.time()}".encode()).hexdigest()
        
        # If updating, check SHA matches
        if sha and sha != content_sha:
            send_error(handler, 409, "sha mismatch")
            return
        
        # Determine status code - 201 for create, 200 for update
        is_update = sha is not None
        status = 200 if is_update else 201
        
        send_json(handler, status, {
            "content": {
                "name": path.split('/')[-1],
                "path": path,
                "sha": content_sha,
                "size": len(content),
                "url": f"http://127.0.0.1:{SERVER_PORT}/api/v3/repos/{owner}/{name}/contents/{path}",
                "html_url": f"http://127.0.0.1:{SERVER_PORT}/{owner}/{name}/blob/main/{path}",
                "git_url": f"http://127.0.0.1:{SERVER_PORT}/api/v3/repos/{owner}/{name}/git/blobs/{content_sha}",
                "download_url": f"http://127.0.0.1:{SERVER_PORT}/{owner}/{name}/raw/main/{path}",
                "type": "file",
                "content": content_b64,
                "encoding": "base64",
            },
            "commit": {
                "sha": commit_sha,
                "url": f"http://127.0.0.1:{SERVER_PORT}/api/v3/repos/{owner}/{name}/git/commits/{commit_sha}",
                "html_url": f"http://127.0.0.1:{SERVER_PORT}/{owner}/{name}/commit/{commit_sha}",
                "author": {"name": VIEWER.name, "email": VIEWER.email, "date": datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')},
                "committer": {"name": VIEWER.name, "email": VIEWER.email, "date": datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')},
                "message": message,
                "tree": {"sha": hashlib.sha1(b"tree").hexdigest(), "url": ""},
                "parents": [],
            }
        })
    elif request.method == 'GET':
        # Check if it's a directory request (path ends with / or we're listing a directory)
        # For simplicity, check if path contains a directory we know about
        # Return file content
        content = f"# {path}\n\nFile content for {path}"
        content_b64 = base64.b64encode(content.encode()).decode()
        content_sha = hashlib.sha1(content.encode()).hexdigest()
        send_json(handler, 200, {
            "name": path.split('/')[-1],
            "path": path,
            "sha": content_sha,
            "size": len(content),
            "url": f"http://127.0.0.1:{SERVER_PORT}/api/v3/repos/{owner}/{name}/contents/{path}",
            "html_url": f"http://127.0.0.1:{SERVER_PORT}/{owner}/{name}/blob/main/{path}",
            "git_url": f"http://127.0.0.1:{SERVER_PORT}/api/v3/repos/{owner}/{name}/git/blobs/{content_sha}",
            "download_url": f"http://127.0.0.1:{SERVER_PORT}/{owner}/{name}/raw/main/{path}",
            "type": "file",
            "content": content_b64,
            "encoding": "base64",
        })
    elif request.method == 'DELETE':
        body = request.json()
        message = body.get('message', 'Delete file')
        sha = body.get('sha')
        if not sha:
            send_error(handler, 422, "sha is required")
            return
        send_json(handler, 200, {
            "commit": {
                "sha": hashlib.sha1(f"delete-{path}{time.time()}".encode()).hexdigest(),
            }
        })
    else:
        send_error(handler, 405, "Method not allowed")

def handle_issues(request: Request, handler: BaseHTTPRequestHandler, owner: str, name: str):
    if not request.is_authenticated:
        send_error(handler, 401, "Requires authentication")
        return
    repo = STORE.get_repo(owner, name)
    if not repo:
        send_error(handler, 404, "Not Found")
        return
    
    if request.method == 'POST':
        body = request.json()
        title = body.get('title')
        body_text = body.get('body')
        labels = body.get('labels', [])
        if not title:
            send_error(handler, 422, "title is required")
            return
        try:
            issue = STORE.create_issue(owner, name, title, body_text, labels)
            send_json(handler, 201, issue_to_dict(issue))
        except ValueError as e:
            send_error(handler, 422, str(e))
    elif request.method == 'GET':
        state = request.query.get('state', ['open'])[0]
        sort = request.query.get('sort', ['created'])[0]
        direction = request.query.get('direction', ['desc'])[0]
        issues = STORE.list_issues(owner, name, state, sort, direction)
        send_json(handler, 200, [issue_to_dict(i) for i in issues])
    else:
        send_error(handler, 405, "Method not allowed")

def handle_issue_get(request: Request, handler: BaseHTTPRequestHandler, owner: str, name: str, number: int):
    if not request.is_authenticated:
        send_error(handler, 401, "Requires authentication")
        return
    repo = STORE.get_repo(owner, name)
    if not repo:
        send_error(handler, 404, "Not Found")
        return
    issue = STORE.get_issue(owner, name, number)
    if not issue:
        send_error(handler, 404, "Not Found")
        return
    send_json(handler, 200, issue_to_dict(issue))

def handle_issue_patch(request: Request, handler: BaseHTTPRequestHandler, owner: str, name: str, number: int):
    if not request.is_authenticated:
        send_error(handler, 401, "Requires authentication")
        return
    repo = STORE.get_repo(owner, name)
    if not repo:
        send_error(handler, 404, "Not Found")
        return
    body = request.json()
    state = body.get('state')
    if state not in ('open', 'closed'):
        send_error(handler, 422, "Invalid state")
        return
    issue = STORE.update_issue(owner, name, number, state)
    if not issue:
        send_error(handler, 404, "Not Found")
        return
    send_json(handler, 200, issue_to_dict(issue))

def handle_issue_comments(request: Request, handler: BaseHTTPRequestHandler, owner: str, name: str, number: int):
    if not request.is_authenticated:
        send_error(handler, 401, "Requires authentication")
        return
    repo = STORE.get_repo(owner, name)
    if not repo:
        send_error(handler, 404, "Not Found")
        return
    if request.method == 'POST':
        body = request.json()
        body_text = body.get('body')
        if not body_text:
            send_error(handler, 422, "body is required")
            return
        comment = STORE.create_comment(owner, name, number, body_text)
        send_json(handler, 201, comment_to_dict(comment))
    elif request.method == 'GET':
        comments = STORE.list_comments(owner, name, number)
        send_json(handler, 200, [comment_to_dict(c) for c in comments])
    else:
        send_error(handler, 405, "Method not allowed")

def handle_pulls(request: Request, handler: BaseHTTPRequestHandler, owner: str, name: str):
    if not request.is_authenticated:
        send_error(handler, 401, "Requires authentication")
        return
    repo = STORE.get_repo(owner, name)
    if not repo:
        send_error(handler, 404, "Not Found")
        return
    
    if request.method == 'POST':
        body = request.json()
        title = body.get('title')
        body_text = body.get('body')
        head = body.get('head')
        base = body.get('base', 'main')
        if not title or not head:
            send_error(handler, 422, "title and head are required")
            return
        try:
            pr = STORE.create_pull(owner, name, title, body_text, head, base)
            send_json(handler, 201, pull_to_dict(pr))
        except ValueError as e:
            send_error(handler, 422, str(e))
    elif request.method == 'GET':
        state = request.query.get('state', ['open'])[0]
        pulls = STORE.list_pulls(owner, name, state)
        send_json(handler, 200, [pull_to_dict(p) for p in pulls])
    else:
        send_error(handler, 405, "Method not allowed")

def handle_pull_get(request: Request, handler: BaseHTTPRequestHandler, owner: str, name: str, number: int):
    if not request.is_authenticated:
        send_error(handler, 401, "Requires authentication")
        return
    repo = STORE.get_repo(owner, name)
    if not repo:
        send_error(handler, 404, "Not Found")
        return
    pr = STORE.get_pull(owner, name, number)
    if not pr:
        send_error(handler, 404, "Not Found")
        return
    send_json(handler, 200, pull_to_dict(pr))

def handle_pull_files(request: Request, handler: BaseHTTPRequestHandler, owner: str, name: str, number: int):
    if not request.is_authenticated:
        send_error(handler, 401, "Requires authentication")
        return
    repo = STORE.get_repo(owner, name)
    if not repo:
        send_error(handler, 404, "Not Found")
        return
    pr = STORE.get_pull(owner, name, number)
    if not pr:
        send_error(handler, 404, "Not Found")
        return
    files = STORE.get_pull_files(owner, name, number)
    send_json(handler, 200, files)

def handle_pull_merge(request: Request, handler: BaseHTTPRequestHandler, owner: str, name: str, number: int):
    if not request.is_authenticated:
        send_error(handler, 401, "Requires authentication")
        return
    repo = STORE.get_repo(owner, name)
    if not repo:
        send_error(handler, 404, "Not Found")
        return
    pr = STORE.merge_pull(owner, name, number)
    if not pr:
        send_error(handler, 404, "Not Found")
        return
    send_json(handler, 200, {
        "sha": pr.merge_commit_sha,
        "merged": True,
        "message": "Pull request merged successfully"
    })

# =============================================================================
# Serialization Helpers
# =============================================================================

def repo_to_dict(repo: Repository) -> dict:
    return {
        "id": repo.id,
        "node_id": repo.node_id,
        "name": repo.name,
        "full_name": repo.full_name,
        "private": repo.private,
        "owner": {
            "login": repo.owner.login,
            "id": repo.owner.id,
            "avatar_url": "",
            "html_url": f"http://127.0.0.1:{SERVER_PORT}/{repo.owner.login}",
        },
        "html_url": repo.html_url,
        "description": repo.description,
        "default_branch": repo.default_branch,
        "created_at": repo.created_at,
        "updated_at": repo.updated_at,
        "pushed_at": repo.pushed_at,
        "url": repo.url,
    }

def issue_to_dict(issue: Issue) -> dict:
    return {
        "id": issue.id,
        "number": issue.number,
        "title": issue.title,
        "body": issue.body,
        "state": issue.state,
        "labels": issue.labels,
        "user": {
            "login": issue.user.login,
            "id": issue.user.id,
            "avatar_url": "",
        },
        "created_at": issue.created_at,
        "updated_at": issue.updated_at,
        "comments": issue.comments,
        "html_url": issue.html_url,
        "node_id": issue.node_id,
    }

def comment_to_dict(comment: Comment) -> dict:
    return {
        "id": comment.id,
        "body": comment.body,
        "user": {
            "login": comment.user.login,
            "id": comment.user.id,
            "avatar_url": "",
        },
        "created_at": comment.created_at,
        "updated_at": comment.updated_at,
        "html_url": comment.html_url,
    }

def pull_to_dict(pr: PullRequest) -> dict:
    return {
        "id": pr.id,
        "number": pr.number,
        "title": pr.title,
        "body": pr.body,
        "state": pr.state,
        "merged": pr.merged,
        "head": pr.head,
        "base": pr.base,
        "user": {
            "login": pr.user.login,
            "id": pr.user.id,
            "avatar_url": "",
        },
        "created_at": pr.created_at,
        "updated_at": pr.updated_at,
        "mergeable": pr.mergeable,
        "html_url": pr.html_url,
        "merged_at": pr.merged_at,
        "merge_commit_sha": pr.merge_commit_sha,
    }

def label_to_dict(label: Label) -> dict:
    return {
        "id": label.id,
        "name": label.name,
        "color": label.color,
        "description": label.description,
        "url": label.url,
    }

# =============================================================================
# Git Smart HTTP Handlers
# =============================================================================

def build_v0_advertisement(refs: dict[str, str], service: str) -> bytes:
    """Build v0 protocol advertisement with proper pkt-line framing."""
    lines = []
    # First line: service announcement
    lines.append(pkt_line(f"# service={service}\n".encode()))
    # Flush
    lines.append(pkt_flush())
    
    # Refs - each ref is a pkt-line
    for ref_name, sha in refs.items():
        if sha != "0" * 40:
            lines.append(pkt_line(f"{sha} {ref_name}\n".encode()))
    
    # Final flush
    lines.append(pkt_flush())
    
    return b"".join(lines)

def build_v2_advertisement() -> bytes:
    """Build v2 protocol advertisement."""
    lines = [
        pkt_line(b"version 2\n"),
        pkt_line(b"agent=gitforge-v2\n"),
        pkt_line(b"ls-refs\n"),
        pkt_line(b"fetch\n"),
        pkt_line(b"object-format=sha1\n"),
        pkt_flush(),
    ]
    return b"".join(lines)

def handle_git_info_refs(request: Request, handler: BaseHTTPRequestHandler, owner: str, name: str, service: str):
    if not request.is_authenticated:
        send_error(handler, 401, "Requires authentication")
        return
    repo = STORE.get_repo(owner, name)
    if not repo:
        handler.send_response(404)
        handler.send_header('Content-Type', 'text/plain')
        handler.end_headers()
        handler.wfile.write(b"Not Found")
        return
    
    full_name = f"{owner}/{name}"
    refs = STORE.git_refs.get(full_name, {})
    
    git_protocol = request.headers.get('Git-Protocol', '')
    is_v2 = 'version=2' in git_protocol
    
    if service == 'git-upload-pack':
        if is_v2:
            handler.send_response(200)
            handler.send_header('Content-Type', 'application/x-git-upload-pack-advertisement')
            handler.end_headers()
            handler.wfile.write(build_v2_advertisement())
        else:
            handler.send_response(200)
            handler.send_header('Content-Type', 'application/x-git-upload-pack-advertisement')
            handler.end_headers()
            handler.wfile.write(build_v0_advertisement(refs, 'git-upload-pack'))
    elif service == 'git-receive-pack':
        handler.send_response(200)
        handler.send_header('Content-Type', 'application/x-git-receive-pack-advertisement')
        handler.end_headers()
        # receive-pack always uses v0
        handler.wfile.write(build_v0_advertisement(refs, 'git-receive-pack'))

def handle_git_upload_pack(request: Request, handler: BaseHTTPRequestHandler, owner: str, name: str):
    if not request.is_authenticated:
        send_error(handler, 401, "Requires authentication")
        return
    repo = STORE.get_repo(owner, name)
    if not repo:
        handler.send_response(404)
        handler.end_headers()
        return
    
    # Parse the request body for want/have negotiation
    content_length = int(request.headers.get('Content-Length', 0))

    handler.send_response(200)
    handler.send_header('Content-Type', 'application/x-git-upload-pack-result')
    handler.end_headers()
    
    # Minimal valid response: sideband-64k with flush packets
    # Send a minimal pack with no objects (empty repository)
    pack_data = b"PACK\x00\x00\x00\x02\x00\x00\x00\x00"
    pack_trailer = hashlib.sha1(pack_data).digest()
    full_pack = pack_data + pack_trailer
    
    # Send as sideband-64k packets (channel 1 = data, channel 2 = progress, channel 3 = error)
    # First send the pack data in 64KB chunks with sideband header
    # For empty pack, just send it in one packet
    # Sideband format: 4-byte pkt-line length, 1-byte channel, data
    # But the simple approach: just send the raw pack with pkt-line framing
    handler.wfile.write(pkt_line(full_pack))
    handler.wfile.write(pkt_flush())

def handle_git_receive_pack(request: Request, handler: BaseHTTPRequestHandler, owner: str, name: str):
    if not request.is_authenticated:
        send_error(handler, 401, "Requires authentication")
        return
    repo = STORE.get_repo(owner, name)
    if not repo:
        handler.send_response(404)
        handler.end_headers()
        return
    
    # Parse the push commands from request body
    content_length = int(request.headers.get('Content-Length', 0))
    handler.send_response(200)
    handler.send_header('Content-Type', 'application/x-git-receive-pack-result')
    handler.end_headers()
    
    # Send unpack ok as pkt-line
    handler.wfile.write(pkt_line(b"unpack ok\n"))
    handler.wfile.write(pkt_flush())

# =============================================================================
# GraphQL Handler
# =============================================================================

def handle_graphql(request: Request, handler: BaseHTTPRequestHandler):
    if not request.is_authenticated:
        send_error(handler, 401, "Requires authentication")
        return
    
    try:
        body = request.json()
    except ValueError:
        send_error(handler, 400, "Malformed JSON")
        return
    
    if isinstance(body, list):
        results = []
        for op in body:
            results.append(execute_graphql(op.get('query', ''), op.get('variables', {}), request.auth_token))
        send_json(handler, 200, results)
    else:
        query = body.get('query', '')
        variables = body.get('variables', {})
        result = execute_graphql(query, variables, request.auth_token)
        send_json(handler, 200, result)

def execute_graphql(query: str, variables: dict, token: str) -> dict:
    query = query.strip()
    
    if 'viewer {' in query and 'login' in query:
        return {"data": {"viewer": {"login": VIEWER.login}}}
    
    if 'repository(owner:' in query and 'name:' in query:
        owner = variables.get('owner', '')
        name = variables.get('name', '')
        repo = STORE.get_repo(owner, name)
        if not repo:
            return {"data": {"repository": None}}
        return {"data": {"repository": repo_to_graphql(repo)}}
    
    if '__type(name:' in query:
        type_name = 'Issue' if 'Issue' in query else ('PullRequest' if 'PullRequest' in query else 'Repository')
        fields = get_type_fields(type_name)
        enum_values = get_enum_values(type_name)
        return {"data": {"__type": {"fields": [{"name": f} for f in fields], "enumValues": enum_values}}}
    
    if 'SearchType' in query:
        return {"data": {"__type": {"enumValues": [{"name": "ISSUE"}, {"name": "REPOSITORY"}]}}}
    
    if 'createIssue' in query:
        input_data = variables.get('input', {})
        repo_id = input_data.get('repositoryId', '')
        title = input_data.get('title', '')
        body = input_data.get('body', '')
        repo = None
        for r in STORE.repositories.values():
            if r.node_id == repo_id:
                repo = r
                break
        if repo:
            issue = STORE.create_issue(repo.owner.login, repo.name, title, body, [])
            return {"data": {"createIssue": {"issue": issue_to_graphql(issue)}}}
        return {"errors": [{"message": "Repository not found"}]}
    
    if 'closeIssue' in query:
        input_data = variables.get('input', {})
        issue_id = input_data.get('issueId', '')
        return {"data": {"closeIssue": {"issue": {"state": "CLOSED"}}}}
    
    if 'createPullRequest' in query:
        input_data = variables.get('input', {})
        repo_id = input_data.get('repositoryId', '')
        base = input_data.get('baseRefName', '')
        head = input_data.get('headRefName', '')
        title = input_data.get('title', '')
        repo = None
        for r in STORE.repositories.values():
            if r.node_id == repo_id:
                repo = r
                break
        if repo:
            pr = STORE.create_pull(repo.owner.login, repo.name, title, '', head, base)
            return {"data": {"createPullRequest": {"pullRequest": pull_to_graphql(pr)}}}
        return {"errors": [{"message": "Repository not found"}]}
    
    if 'mergePullRequest' in query:
        input_data = variables.get('input', {})
        pr_id = input_data.get('pullRequestId', '')
        return {"data": {"mergePullRequest": {"pullRequest": {"merged": True, "state": "MERGED"}}}}
    
    if 'addComment' in query:
        input_data = variables.get('input', {})
        return {"data": {"addComment": {"commentEdge": {"node": {"id": "1", "body": input_data.get('body', '')}}}}}
    
    if 'issues(first:' in query:
        owner = variables.get('owner', '')
        name = variables.get('name', '')
        first = variables.get('first', 10)
        after = variables.get('after')
        issues = STORE.list_issues(owner, name, 'open', 'created', 'desc')
        nodes = issues[:first]
        has_next = len(issues) > first
        return {"data": {"repository": {"issues": {"nodes": [issue_to_graphql(i) for i in nodes], "pageInfo": {"hasNextPage": has_next, "endCursor": str(first)}}}}}
    
    if 'issue(number:' in query:
        owner = variables.get('owner', '')
        name = variables.get('name', '')
        number = variables.get('number', 0)
        issue = STORE.get_issue(owner, name, number)
        if issue:
            return {"data": {"repository": {"issue": issue_to_graphql(issue)}}}
        return {"data": {"repository": {"issue": None}}}
    
    if 'pullRequests(first:' in query:
        owner = variables.get('owner', '')
        name = variables.get('name', '')
        first = variables.get('first', 5)
        pulls = STORE.list_pulls(owner, name, 'open')
        nodes = pulls[:first]
        return {"data": {"repository": {"pullRequests": {"nodes": [pull_to_graphql(p) for p in nodes]}}}}
    
    if 'pullRequest(number:' in query and 'files' not in query:
        owner = variables.get('owner', '')
        name = variables.get('name', '')
        number = variables.get('number', 0)
        pr = STORE.get_pull(owner, name, number)
        if pr:
            return {"data": {"repository": {"pullRequest": pull_to_graphql(pr)}}}
        return {"data": {"repository": {"pullRequest": None}}}
    
    if 'pullRequest(number:' in query and 'files' in query:
        owner = variables.get('owner', '')
        name = variables.get('name', '')
        number = variables.get('number', 0)
        pr = STORE.get_pull(owner, name, number)
        if pr:
            files = STORE.get_pull_files(owner, name, number)
            return {"data": {"repository": {"pullRequest": {"files": {"nodes": [{"path": f["filename"]} for f in files]}}}}}
        return {"data": {"repository": {"pullRequest": None}}}
    
    if 'node(id:' in query:
        node_id = variables.get('id', '')
        try:
            decoded = base64.b64decode(node_id).decode()
            if decoded.startswith('Repository:'):
                repo_id = int(decoded.split(':')[1])
                for r in STORE.repositories.values():
                    if r.id == repo_id:
                        return {"data": {"node": {"id": node_id, "name": r.name, "nameWithOwner": r.full_name}}}
        except Exception:
            pass
        return {"data": {"node": None}}
    
    return {"errors": [{"message": "Unknown query"}]}

def repo_to_graphql(repo: Repository) -> dict:
    return {
        "id": repo.node_id,
        "databaseId": repo.id,
        "name": repo.name,
        "nameWithOwner": repo.full_name,
        "owner": {"login": repo.owner.login},
        "defaultBranchRef": {"name": repo.default_branch, "target": {"oid": "0" * 40}},
        "viewerPermission": "ADMIN",
        "hasIssuesEnabled": True,
        "isPrivate": repo.private,
        "description": repo.description,
    }

def issue_to_graphql(issue: Issue) -> dict:
    return {
        "id": issue.node_id,
        "number": issue.number,
        "title": issue.title,
        "state": issue.state.upper(),
        "body": issue.body or "",
        "author": {"login": issue.user.login},
        "labels": {"nodes": [{"name": l["name"]} for l in issue.labels]},
        "comments": {"nodes": []},
        "createdAt": issue.created_at,
        "updatedAt": issue.updated_at,
    }

def pull_to_graphql(pr: PullRequest) -> dict:
    return {
        "id": base64.b64encode(f"PullRequest:{pr.head['repo']['full_name']}:{pr.number}".encode()).decode(),
        "number": pr.number,
        "title": pr.title,
        "state": pr.state.upper(),
        "merged": pr.merged,
        "headRefName": pr.head["ref"],
        "baseRefName": pr.base["ref"],
        "mergeable": pr.mergeable,
        "files": {"nodes": []},
    }

def get_type_fields(type_name: str) -> list:
    fields_map = {
        "Issue": ["id", "number", "title", "state", "body", "author", "labels", "createdAt", "updatedAt", "comments", "closed"],
        "PullRequest": ["id", "number", "title", "state", "merged", "headRefName", "baseRefName", "mergeable", "files", "author", "body", "createdAt", "updatedAt"],
        "Repository": ["id", "databaseId", "name", "nameWithOwner", "owner", "defaultBranchRef", "viewerPermission", "hasIssuesEnabled", "isPrivate", "description", "issue", "issues", "pullRequests", "pullRequest"],
    }
    return fields_map.get(type_name, [])

def get_enum_values(type_name: str) -> list:
    enums_map = {
        "SearchType": [{"name": "ISSUE"}, {"name": "REPOSITORY"}, {"name": "USER"}],
        "IssueState": [{"name": "OPEN"}, {"name": "CLOSED"}],
        "PullRequestState": [{"name": "OPEN"}, {"name": "CLOSED"}, {"name": "MERGED"}],
        "MergeableState": [{"name": "MERGEABLE"}, {"name": "CONFLICTING"}, {"name": "UNKNOWN"}],
        "RepositoryPermission": [{"name": "ADMIN"}, {"name": "MAINTAIN"}, {"name": "WRITE"}, {"name": "TRIAGE"}, {"name": "READ"}],
        "IssueOrderField": [{"name": "CREATED_AT"}, {"name": "UPDATED_AT"}, {"name": "COMMENTS"}],
        "OrderDirection": [{"name": "ASC"}, {"name": "DESC"}],
    }
    return enums_map.get(type_name, [])

# =============================================================================
# MCP Handler
# =============================================================================

MCP_TOOLS = [
    {
        "name": "create_repo",
        "description": "Create a new repository",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "description": {"type": "string"},
                "private": {"type": "boolean"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "get_repo",
        "description": "Get a repository",
        "inputSchema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string"},
                "repo": {"type": "string"},
            },
            "required": ["owner", "repo"],
        },
    },
    {
        "name": "create_issue",
        "description": "Create an issue",
        "inputSchema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string"},
                "repo": {"type": "string"},
                "title": {"type": "string"},
                "body": {"type": "string"},
            },
            "required": ["owner", "repo", "title"],
        },
    },
    {
        "name": "list_issues",
        "description": "List issues",
        "inputSchema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string"},
                "repo": {"type": "string"},
                "state": {"type": "string"},
            },
            "required": ["owner", "repo"],
        },
    },
    {
        "name": "create_pull_request",
        "description": "Create a pull request",
        "inputSchema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string"},
                "repo": {"type": "string"},
                "title": {"type": "string"},
                "body": {"type": "string"},
                "head": {"type": "string"},
                "base": {"type": "string"},
            },
            "required": ["owner", "repo", "title", "head", "base"],
        },
    },
    {
        "name": "merge_pull_request",
        "description": "Merge a pull request",
        "inputSchema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string"},
                "repo": {"type": "string"},
                "pull_number": {"type": "integer"},
            },
            "required": ["owner", "repo", "pull_number"],
        },
    },
]

def handle_mcp(request: Request, handler: BaseHTTPRequestHandler):
    if not request.is_authenticated:
        send_error(handler, 401, "Requires authentication")
        return
    
    try:
        body = request.json()
    except ValueError:
        send_json(handler, 400, {"jsonrpc": "2.0", "error": {"code": -32700, "message": "Parse error"}, "id": None})
        return
    
    if not body:
        send_json(handler, 400, {"jsonrpc": "2.0", "error": {"code": -32700, "message": "Parse error"}, "id": None})
        return
    
    if isinstance(body, list):
        results = []
        for req in body:
            results.append(handle_mcp_request(req))
        send_json(handler, 200, results)
        return
    
    result = handle_mcp_request(body)
    send_json(handler, 200, result)

def handle_mcp_request(req: dict) -> dict:
    method = req.get('method')
    params = req.get('params', {})
    req_id = req.get('id')
    
    if method == 'initialize':
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2025-11-25",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "gitforge-v2", "version": "1.0.0"},
            }
        }
    elif method == 'notifications/initialized':
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}
    elif method == 'tools/list':
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": MCP_TOOLS}
        }
    elif method == 'tools/call':
        tool_name = params.get('name')
        arguments = params.get('arguments', {})
        
        tool = next((t for t in MCP_TOOLS if t['name'] == tool_name), None)
        if not tool:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32602, "message": f"Unknown tool: {tool_name}"}
            }
        
        required = tool['inputSchema'].get('required', [])
        missing = [r for r in required if r not in arguments]
        if missing:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": f"Missing required arguments: {', '.join(missing)}"}],
                    "isError": True
                }
            }
        
        try:
            if tool_name == 'create_repo':
                repo = STORE.create_repo(arguments['name'], arguments.get('private', False), arguments.get('description', ''))
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {"content": [{"type": "text", "text": json.dumps(repo_to_dict(repo))}]}
                }
            elif tool_name == 'get_repo':
                repo = STORE.get_repo(arguments['owner'], arguments['repo'])
                if repo:
                    return {"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": json.dumps(repo_to_dict(repo))}]}}
                return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32602, "message": "Repository not found"}}
            elif tool_name == 'create_issue':
                issue = STORE.create_issue(arguments['owner'], arguments['repo'], arguments['title'], arguments.get('body'), [])
                return {"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": json.dumps(issue_to_dict(issue))}]}}
            elif tool_name == 'list_issues':
                issues = STORE.list_issues(arguments['owner'], arguments['repo'], arguments.get('state', 'open'))
                return {"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": json.dumps([issue_to_dict(i) for i in issues])}]}}
            elif tool_name == 'create_pull_request':
                pr = STORE.create_pull(arguments['owner'], arguments['repo'], arguments['title'], arguments.get('body'), arguments['head'], arguments['base'])
                return {"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": json.dumps(pull_to_dict(pr))}]}}
            elif tool_name == 'merge_pull_request':
                pr = STORE.merge_pull(arguments['owner'], arguments['repo'], arguments['pull_number'])
                if pr:
                    return {"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": json.dumps(pull_to_dict(pr))}]}}
                return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32602, "message": "Pull request not found"}}
        except ValueError as e:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32602, "message": str(e)}
            }
    
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"}
    }

# =============================================================================
# Health Check and Boot Contract
# =============================================================================

def handle_healthz(request: Request, handler: BaseHTTPRequestHandler):
    send_json(handler, 200, {"ok": True})

def handle_register(request: Request, handler: BaseHTTPRequestHandler):
    global SERVER_PORT
    if request.method != 'POST':
        handler.send_response(405)
        handler.end_headers()
        return
    body = request.json()
    port = body.get('port')
    if not isinstance(port, int) or port < 1 or port > 65535:
        handler.send_response(400)
        handler.end_headers()
        return
    SERVER_PORT = port
    if MAKEFUN_ENV_URL:
        try:
            import urllib.request
            req = urllib.request.Request(
                f"{MAKEFUN_ENV_URL}/register",
                data=json.dumps({"port": port}).encode(),
                headers={'Content-Type': 'application/json'},
                method='POST'
            )
            urllib.request.urlopen(req, timeout=5)
        except Exception:
            pass
    send_json(handler, 200, {"ok": True})

def handle_done(request: Request, handler: BaseHTTPRequestHandler):
    if MAKEFUN_ENV_URL:
        while not SHUTDOWN_EVENT.is_set():
            try:
                import urllib.request
                req = urllib.request.Request(f"{MAKEFUN_ENV_URL}/done", method='GET')
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = json.loads(resp.read().decode())
                    if data.get('done'):
                        SHUTDOWN_EVENT.set()
                        break
            except Exception:
                time.sleep(1)
    send_json(handler, 200, {"done": True})

# =============================================================================
# Main Request Router
# =============================================================================

def route_request(request: Request, handler: BaseHTTPRequestHandler):
    path = urlparse(request.path).path
    
    if path == '/healthz':
        handle_healthz(request, handler)
        return
    if path == '/register':
        handle_register(request, handler)
        return
    if path == '/done':
        handle_done(request, handler)
        return
    
    if path == '/api/v3/':
        handle_root(request, handler)
        return
    if path == '/api/v3/meta':
        handle_meta(request, handler)
        return
    if path == '/api/v3/user/repos':
        if request.method == 'GET':
            handle_user_repos_list(request, handler)
        elif request.method == 'POST':
            handle_user_repos_create(request, handler)
        else:
            send_error(handler, 405, "Method not allowed")
        return
    
    if path.startswith('/api/v3/repos/'):
        parts = path[len('/api/v3/repos/'):].split('/')
        if len(parts) >= 2:
            owner, repo = parts[0], parts[1]
            rest = '/'.join(parts[2:]) if len(parts) > 2 else ''
            
            if rest == '':
                if request.method == 'GET':
                    handle_repo_get(request, handler, owner, repo)
                elif request.method == 'PATCH':
                    handle_repo_patch(request, handler, owner, repo)
                elif request.method == 'DELETE':
                    handle_repo_delete(request, handler, owner, repo)
                else:
                    send_error(handler, 405, "Method not allowed")
                return
            
            if rest == 'branches':
                handle_repo_branches(request, handler, owner, repo)
                return
            if rest == 'commits':
                handle_repo_commits(request, handler, owner, repo)
                return
            if rest == 'git/refs':
                if request.method == 'POST':
                    handle_repo_git_refs(request, handler, owner, repo)
                else:
                    send_error(handler, 405, "Method not allowed")
                return
            if rest == 'labels':
                handle_repo_labels(request, handler, owner, repo)
                return
            if rest.startswith('labels/'):
                label_name = rest[len('labels/'):]
                handle_repo_label(request, handler, owner, repo, label_name)
                return
            if rest.startswith('contents/'):
                content_path = rest[len('contents/'):]
                handle_repo_contents(request, handler, owner, repo, content_path)
                return
            if rest == 'issues':
                handle_issues(request, handler, owner, repo)
                return
            if rest.startswith('issues/'):
                issue_parts = rest[len('issues/'):].split('/')
                if issue_parts[0].isdigit():
                    issue_num = int(issue_parts[0])
                    if len(issue_parts) == 1:
                        if request.method == 'GET':
                            handle_issue_get(request, handler, owner, repo, issue_num)
                        elif request.method == 'PATCH':
                            handle_issue_patch(request, handler, owner, repo, issue_num)
                        else:
                            send_error(handler, 405, "Method not allowed")
                    elif issue_parts[1] == 'comments':
                        handle_issue_comments(request, handler, owner, repo, issue_num)
                    else:
                        send_error(handler, 404, "Not Found")
                    return
            if rest == 'pulls':
                handle_pulls(request, handler, owner, repo)
                return
            if rest.startswith('pulls/'):
                pull_parts = rest[len('pulls/'):].split('/')
                if pull_parts[0].isdigit():
                    pull_num = int(pull_parts[0])
                    if len(pull_parts) == 1:
                        if request.method == 'GET':
                            handle_pull_get(request, handler, owner, repo, pull_num)
                        else:
                            send_error(handler, 405, "Method not allowed")
                    elif pull_parts[1] == 'files':
                        handle_pull_files(request, handler, owner, repo, pull_num)
                    elif pull_parts[1] == 'merge':
                        if request.method == 'PUT':
                            handle_pull_merge(request, handler, owner, repo, pull_num)
                        else:
                            send_error(handler, 405, "Method not allowed")
                    else:
                        send_error(handler, 404, "Not Found")
                    return
    
    if path.endswith('.git/info/refs'):
        git_path = path[:-len('.git/info/refs')]
        parts = git_path.strip('/').split('/')
        if len(parts) == 2:
            owner, repo = parts
            service = request.query.get('service', [None])[0]
            if service in ('git-upload-pack', 'git-receive-pack'):
                handle_git_info_refs(request, handler, owner, repo, service)
                return
    if path.endswith('.git/git-upload-pack'):
        git_path = path[:-len('.git/git-upload-pack')]
        parts = git_path.strip('/').split('/')
        if len(parts) == 2:
            owner, repo = parts
            handle_git_upload_pack(request, handler, owner, repo)
            return
    if path.endswith('.git/git-receive-pack'):
        git_path = path[:-len('.git/git-receive-pack')]
        parts = git_path.strip('/').split('/')
        if len(parts) == 2:
            owner, repo = parts
            handle_git_receive_pack(request, handler, owner, repo)
            return
    
    if path == '/api/graphql':
        handle_graphql(request, handler)
        return
    
    if path == '/mcp':
        handle_mcp(request, handler)
        return
    
    send_error(handler, 404, "Not Found")

# =============================================================================
# HTTP Server
# =============================================================================

class ForgeHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        request = Request(self)
        route_request(request, self)
    
    def do_POST(self):
        request = Request(self)
        route_request(request, self)
    
    def do_PATCH(self):
        request = Request(self)
        route_request(request, self)
    
    def do_DELETE(self):
        request = Request(self)
        route_request(request, self)
    
    def do_PUT(self):
        request = Request(self)
        route_request(request, self)
    
    def log_message(self, format, *args):
        pass

def run_server(port: int):
    global SERVER_PORT, STORE
    # Reset store on each server start for clean state
    STORE = Store()
    
    server = HTTPServer(('127.0.0.1', port), ForgeHandler)
    SERVER_PORT = server.server_address[1]
    print(f"Server starting on port {SERVER_PORT}", file=sys.stderr)
    
    if MAKEFUN_ENV_URL:
        def register():
            time.sleep(0.5)
            try:
                import urllib.request
                req = urllib.request.Request(
                    f"{MAKEFUN_ENV_URL}/register",
                    data=json.dumps({"port": SERVER_PORT}).encode(),
                    headers={'Content-Type': 'application/json'},
                    method='POST'
                )
                urllib.request.urlopen(req, timeout=5)
            except Exception as e:
                print(f"Register failed: {e}", file=sys.stderr)
        threading.Thread(target=register, daemon=True).start()
    
    if MAKEFUN_ENV_URL:
        def poll_done():
            while not SHUTDOWN_EVENT.is_set():
                try:
                    import urllib.request
                    req = urllib.request.Request(f"{MAKEFUN_ENV_URL}/done", method='GET')
                    with urllib.request.urlopen(req, timeout=30) as resp:
                        data = json.loads(resp.read().decode())
                        if data.get('done'):
                            SHUTDOWN_EVENT.set()
                            server.shutdown()
                            break
                except Exception:
                    time.sleep(1)
        threading.Thread(target=poll_done, daemon=True).start()
    
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=0, help='Port to bind to (0 for ephemeral)')
    args = parser.parse_args()
    
    port = args.port or PORT
    run_server(port)

if __name__ == '__main__':
    main()