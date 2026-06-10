#!/usr/bin/env python3
"""
scrub.py

Steam/Proton log scrubber engine.

Expected public API for main.py:

    load_rules(path)
    scrub_folder(input_dir, output_dir, rules, dry_run=False, force=False)

This module intentionally keeps Steam-specific contextual logic in Python while
allowing JSON profiles to guide behavior, enable/disable built-ins, define file
classes, domain policy, discovery rules, preserve rules, and explicit regex
redaction rules.
"""

from __future__ import annotations

import fnmatch
import ipaddress
import json
import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


DEFAULT_REPLACEMENT = "REDACTED"

DEFAULT_PRESERVE_DOMAINS = [
    "steamstatic.com",
    "steamcontent.com",
    "steampowered.com",
    "steamcommunity.com",
    "steamserver.net",
    "valvesoftware.com",
    "steamloopback.host",
    "steamconnecttest.com",
    "localhost",
    "disabled.invalid",
    "chromestatus.com",
    "chromium.org",
    "www.chromium.org",
    "developer.chrome.com",
    "specifications.freedesktop.org",
    "akamaihd.net",
    "akamai.net",
]

DEFAULT_REDACT_DOMAINS = [
    "youtube.com",
    "youtu.be",
    "googlevideo.com",
]

DEFAULT_HIGH_NETWORK_FILES = [
    "connection_log.txt",
    "remote_connections.txt",
    "transport_client.txt",
    "transport_steamui.txt",
    "content_log.txt",
    "console-linux.txt",
    "console-linux.previous.txt",
    "console_log.txt",
    "console_log.previous.txt",
    "cloud_log.txt",
    "cloud_log.previous.txt",
    "webhelper.txt",
    "webhelper-linux.txt",
    "webhelper_js.txt",
    "webhelper_gpu.txt",
    "cef_log.txt",
    "steamui_html.txt",
]

SAFE_ACCOUNT_WORDS = {
    "steam",
    "steamui",
    "login",
    "user",
    "none",
    "null",
    "true",
    "false",
    "anonymous",
    "default",
    "public",
    "private",
    "local",
    "localhost",
    "unknown",
    "undefined",
    "redacted",
    "account",
    "client",
    "server",
    "persona",
    "name",
}

DEFAULT_SENSITIVE_QUERY_KEYS = {
    "access_token",
    "refresh_token",
    "token",
    "session",
    "sessionid",
    "auth",
    "oauth",
    "key",
    "apikey",
    "api_key",
    "password",
    "passwd",
    "secret",
    "steamid",
    "accountid",
    "ticket",
    "webcookie",
    "login",
}

DEFAULT_SENSITIVE_FIELD_KEYS = [
    "oauth",
    "access_token",
    "refresh_token",
    "sessionid",
    "machineauth",
    "loginkey",
    "apikey",
    "api_key",
    "ticket",
    "webcookie",
    "sentry",
    "client_secret",
    "deviceid",
    "device_id",
    "machineid",
    "machine_id",
    "installid",
    "install_id",
    "sessionkey",
    "session_key",
]

DEFAULT_ACCOUNT_FIELD_KEYS = [
    "accountid",
    "account_id",
    "steamid",
    "steam_id",
    "steamid64",
    "personaname",
    "persona_name",
    "accountname",
    "account_name",
    "loginusername",
    "login_user",
    "userid",
    "user_id",
]

DEFAULT_PRIVATE_GUID_KEYS = [
    "machine_guid",
    "user_guid",
    "device_guid",
    "deviceguid",
    "install_guid",
    "installguid",
    "client_guid",
    "clientguid",
    "account_guid",
    "accountguid",
]


@dataclass
class ScrubResult:
    input_dir: Path
    output_dir: Path
    rules_name: str = "unknown"

    files_scanned: int = 0
    text_files: int = 0
    binary_files: int = 0
    files_changed: int = 0

    redactions: int = 0
    redaction_counts: dict[str, int] = field(default_factory=dict)

    account_names_detected: int = 0
    discovered_value_counts: dict[str, int] = field(default_factory=dict)

    warnings: list[str] = field(default_factory=list)
    leftovers: list[str] = field(default_factory=list)

    output_backup: Path | None = None
    dry_run: bool = False


@dataclass
class ScrubContext:
    rules: dict[str, Any]
    replacement: str
    preserve_domains: list[str]
    redact_domains: list[str]
    high_network_files: set[str]
    discovered_values: dict[str, set[str]] = field(default_factory=dict)
    redaction_counts: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def add_redactions(self, rule_id: str, count: int) -> None:
        if count <= 0:
            return
        self.redaction_counts[rule_id] = self.redaction_counts.get(rule_id, 0) + count


def load_rules(path: str | Path) -> dict[str, Any]:
    rules_path = Path(path).expanduser().resolve()

    with rules_path.open("r", encoding="utf-8") as file:
        rules = json.load(file)

    if not isinstance(rules, dict):
        raise ValueError(f"Rules file must contain a JSON object: {rules_path}")

    rules.setdefault("_path", str(rules_path))
    rules.setdefault("profile_name", rules_path.stem)

    return rules


def get_nested(data: dict[str, Any], dotted: str, default: Any = None) -> Any:
    current: Any = data

    for key in dotted.split("."):
        if not isinstance(current, dict) or key not in current:
            return default
        current = current[key]

    return current


def builtin_enabled(rules: dict[str, Any], section: str, default: bool = True) -> bool:
    return bool(get_nested(rules, f"builtins.{section}.enabled", default))


def builtin_option(
    rules: dict[str, Any],
    section: str,
    option: str,
    default: Any = None,
) -> Any:
    return get_nested(rules, f"builtins.{section}.{option}", default)


def is_binary(data: bytes) -> bool:
    if b"\x00" in data:
        return True

    sample = data[:4096]
    if not sample:
        return False

    control_count = sum(1 for byte in sample if byte < 9 or (13 < byte < 32))
    return control_count / max(len(sample), 1) > 0.20


def decode_text(data: bytes) -> str:
    return data.decode("utf-8", errors="surrogateescape")


def encode_text(text: str) -> bytes:
    return text.encode("utf-8", errors="surrogateescape")


def timestamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def backup_existing_output(output_dir: Path, force: bool) -> Path | None:
    if not output_dir.exists():
        return None

    if force:
        shutil.rmtree(output_dir)
        return None

    backup_path = output_dir.with_name(f"{output_dir.name}.bak-{timestamp()}")
    counter = 2

    while backup_path.exists():
        backup_path = output_dir.with_name(f"{output_dir.name}.bak-{timestamp()}-{counter}")
        counter += 1

    shutil.move(str(output_dir), str(backup_path))
    return backup_path


def valid_account_name(name: str, rules: dict[str, Any]) -> bool:
    cleaned = name.strip().strip("\"'<>[]{}(),;")
    low = cleaned.lower()

    min_len = int(builtin_option(rules, "steam_account_name_discovery", "min_length", 3))
    max_len = int(builtin_option(rules, "steam_account_name_discovery", "max_length", 64))

    if len(cleaned) < min_len or len(cleaned) > max_len:
        return False

    if low in SAFE_ACCOUNT_WORDS:
        return False

    if cleaned.isdigit():
        return False

    if not re.search(r"[A-Za-z]", cleaned):
        return False

    if not re.fullmatch(r"[A-Za-z0-9_.-]+", cleaned):
        return False

    return True


def compile_flags(rule: dict[str, Any]) -> int:
    flags = 0

    for item in rule.get("flags", []):
        item = str(item).lower().strip()

        if item in {"i", "ignorecase", "ignore_case"}:
            flags |= re.IGNORECASE
        elif item in {"m", "multiline"}:
            flags |= re.MULTILINE
        elif item in {"s", "dotall", "singleline"}:
            flags |= re.DOTALL
        elif item in {"a", "ascii"}:
            flags |= re.ASCII

    return flags


def rule_enabled(rule: dict[str, Any]) -> bool:
    return bool(rule.get("enabled", True))


def path_matches_any(relative_path: str, filename: str, patterns: list[str]) -> bool:
    for pattern in patterns:
        if fnmatch.fnmatch(filename, pattern) or fnmatch.fnmatch(relative_path, pattern):
            return True
    return False


def rule_applies_to_file(
    rule: dict[str, Any],
    filename: str,
    relative_path: str,
    context: ScrubContext,
) -> bool:
    include_files = rule.get("include_files") or []
    exclude_files = rule.get("exclude_files") or []

    if include_files and not path_matches_any(relative_path, filename, include_files):
        return False

    if exclude_files and path_matches_any(relative_path, filename, exclude_files):
        return False

    scope = rule.get("scope", "all_text_files")

    if scope in {"all", "all_text_files", "*"}:
        return True

    if scope == "high_network":
        return filename in context.high_network_files

    if scope == "non_high_network":
        return filename not in context.high_network_files

    if isinstance(scope, list):
        return path_matches_any(relative_path, filename, scope)

    return True


def host_matches_domain_list(host: str | None, domains: list[str]) -> bool:
    normalized = (host or "").lower().strip(".")

    for domain in domains:
        allowed = domain.lower().strip(".")
        if normalized == allowed or normalized.endswith("." + allowed):
            return True

    return False


def host_is_ip(host: str | None) -> bool:
    if not host:
        return False

    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def host_is_safe_placeholder_ip(host: str | None, rules: dict[str, Any]) -> bool:
    if not host:
        return False

    preserve_loopback = bool(builtin_option(rules, "network", "preserve_loopback", True))
    preserve_zero = bool(builtin_option(rules, "network", "preserve_zero_placeholders", True))

    if preserve_zero and host == "0.0.0.0":
        return True

    if preserve_loopback:
        if host == "localhost":
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False

    return False


def protect_preserve_rules(
    text: str,
    filename: str,
    relative_path: str,
    context: ScrubContext,
) -> tuple[str, dict[str, str]]:
    preserve_map: dict[str, str] = {}
    counter = 0

    for rule in context.rules.get("preserve_rules", []):
        if not isinstance(rule, dict) or not rule_enabled(rule):
            continue

        if not rule_applies_to_file(rule, filename, relative_path, context):
            continue

        pattern_text = rule.get("regex")
        if not pattern_text:
            continue

        try:
            pattern = re.compile(pattern_text, compile_flags(rule))
        except re.error as exc:
            context.warnings.append(f"Invalid preserve regex in {rule.get('id', 'unnamed')}: {exc}")
            continue

        def repl(match: re.Match) -> str:
            nonlocal counter
            key = f"\uE000STEAMLOGSCRUB_PRESERVE_{counter:08d}\uE000"
            preserve_map[key] = match.group(0)
            counter += 1
            return key

        text = pattern.sub(repl, text)

    return text, preserve_map


def restore_preserved_text(text: str, preserve_map: dict[str, str]) -> str:
    for placeholder, original in preserve_map.items():
        text = text.replace(placeholder, original)
    return text


def discover_values_from_text(
    text: str,
    filename: str,
    relative_path: str,
    context: ScrubContext,
) -> None:
    rules = context.rules

    if builtin_enabled(rules, "steam_account_name_discovery", True):
        builtin_discovery_rules = [
            {
                "id": "builtin_steamui_on_login_state_change",
                "regex": r"\bOnLoginStateChange\s+([A-Za-z0-9_.-]{3,64})(?=\s+[-0-9])",
                "capture_group": 1,
                "value_type": "steam_account_name",
                "scope": "all_text_files",
            },
            {
                "id": "builtin_steam_account_name_labeled",
                "regex": (
                    r"\b(?:AccountName|account_name|accountname|PersonaName|persona_name|"
                    r"personaname|LoginUserName|loginusername)\b\s*[:=]\s*[\"']?"
                    r"([A-Za-z0-9_.-]{3,64})"
                ),
                "capture_group": 1,
                "value_type": "steam_account_name",
                "scope": "all_text_files",
                "flags": ["ignorecase"],
            },
        ]
    else:
        builtin_discovery_rules = []

    discovery_rules = builtin_discovery_rules + [
        rule for rule in context.rules.get("discovery_rules", []) if isinstance(rule, dict)
    ]

    for rule in discovery_rules:
        if not rule_enabled(rule):
            continue

        if not rule_applies_to_file(rule, filename, relative_path, context):
            continue

        pattern_text = rule.get("regex")
        if not pattern_text:
            continue

        capture_group = int(rule.get("capture_group", 1))
        value_type = str(rule.get("value_type", "discovered_value"))

        try:
            pattern = re.compile(pattern_text, compile_flags(rule))
        except re.error as exc:
            context.warnings.append(f"Invalid discovery regex in {rule.get('id', 'unnamed')}: {exc}")
            continue

        for match in pattern.finditer(text):
            try:
                value = match.group(capture_group)
            except IndexError:
                context.warnings.append(
                    f"Discovery rule {rule.get('id', 'unnamed')} requested missing capture group {capture_group}"
                )
                continue

            value = value.strip().strip("\"'<>[]{}(),;")
            if not value:
                continue

            if value_type in {"steam_account_name", "account_name", "persona_name"}:
                if not valid_account_name(value, rules):
                    continue
                value_type = "steam_account_name"

            context.discovered_values.setdefault(value_type, set()).add(value)


def discover_values(input_dir: Path, context: ScrubContext) -> None:
    for path in input_dir.rglob("*"):
        if not path.is_file():
            continue

        try:
            data = path.read_bytes()
        except OSError as exc:
            context.warnings.append(f"Could not read during discovery: {path}: {exc}")
            continue

        if is_binary(data):
            continue

        relative_path = str(path.relative_to(input_dir))
        filename = path.name
        text = decode_text(data)

        discover_values_from_text(text, filename, relative_path, context)


def redact_discovered_values(text: str, context: ScrubContext) -> str:
    rules = context.rules
    replacement = context.replacement

    redact_names_globally = bool(
        builtin_option(
            rules,
            "steam_account_name_discovery",
            "redact_discovered_names_globally",
            True,
        )
    )

    for value_type, values in context.discovered_values.items():
        if value_type == "steam_account_name" and not redact_names_globally:
            continue

        for value in sorted(values, key=len, reverse=True):
            if len(value) < 3:
                continue

            if value_type == "steam_account_name":
                pattern = re.compile(
                    rf"(?<![A-Za-z0-9_.-]){re.escape(value)}(?![A-Za-z0-9_.-])",
                    re.IGNORECASE,
                )
            else:
                pattern = re.compile(re.escape(value))

            text, count = pattern.subn(replacement, text)
            context.add_redactions(f"discovered_{value_type}", count)

    return text


def apply_regex_rules_by_phase(
    text: str,
    filename: str,
    relative_path: str,
    context: ScrubContext,
    phases: set[str],
) -> str:
    for rule in context.rules.get("redaction_rules", []):
        if not isinstance(rule, dict) or not rule_enabled(rule):
            continue

        phase = str(rule.get("phase", "main")).lower()
        if phase not in phases:
            continue

        if not rule_applies_to_file(rule, filename, relative_path, context):
            continue

        pattern_text = rule.get("regex")
        if not pattern_text:
            continue

        replacement = rule.get("replacement", context.replacement)
        rule_id = str(rule.get("id", "unnamed_regex_rule"))

        try:
            pattern = re.compile(pattern_text, compile_flags(rule))
        except re.error as exc:
            context.warnings.append(f"Invalid redaction regex in {rule_id}: {exc}")
            continue

        text, count = pattern.subn(replacement, text)
        context.add_redactions(rule_id, count)

    return text


def redact_url(match: re.Match, high_network: bool, context: ScrubContext) -> str:
    rules = context.rules
    replacement = context.replacement

    full = match.group(0)
    raw = full.rstrip(").,;\"'")
    trailing = full[len(raw):]

    try:
        parsed = urlsplit(raw)
    except Exception:
        return replacement + trailing

    host = parsed.hostname or ""

    urls_enabled = builtin_enabled(rules, "urls", True)
    network_enabled = builtin_enabled(rules, "network", True)

    redact_video_tracking_urls = bool(
        builtin_option(rules, "urls", "redact_video_tracking_urls", True)
    )
    redact_sensitive_query_values = bool(
        builtin_option(rules, "urls", "redact_sensitive_query_values", True)
    )
    redact_non_steam_high_network = bool(
        builtin_option(
            rules,
            "urls",
            "redact_non_steam_urls_in_high_network_logs",
            True,
        )
    )
    redact_ip_url_hosts = bool(
        builtin_option(rules, "network", "redact_ip_url_hosts", True)
    )

    if urls_enabled and redact_video_tracking_urls:
        if host_matches_domain_list(host, context.redact_domains):
            return replacement + trailing

    if network_enabled and host_is_safe_placeholder_ip(host, rules):
        return full

    if network_enabled and redact_ip_url_hosts and host_is_ip(host):
        return replacement + trailing

    changed = False
    query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
    new_pairs = []

    sensitive_query_keys = set(
        get_nested(rules, "query_policy.sensitive_query_keys", DEFAULT_SENSITIVE_QUERY_KEYS)
    )

    if urls_enabled and redact_sensitive_query_values:
        for key, value in query_pairs:
            if key.lower() in sensitive_query_keys:
                new_pairs.append((key, replacement))
                changed = True
            else:
                new_pairs.append((key, value))
    else:
        new_pairs = query_pairs

    preserve_steam_infra = bool(
        builtin_option(rules, "network", "preserve_steam_infrastructure", True)
    )

    if (
        urls_enabled
        and high_network
        and redact_non_steam_high_network
        and not host_matches_domain_list(host, context.preserve_domains)
    ):
        return replacement + trailing

    if preserve_steam_infra and host_matches_domain_list(host, context.preserve_domains):
        if changed:
            return urlunsplit(
                (
                    parsed.scheme,
                    parsed.netloc,
                    parsed.path,
                    urlencode(new_pairs, doseq=True),
                    parsed.fragment,
                )
            ) + trailing
        return full

    if changed:
        return urlunsplit(
            (
                parsed.scheme,
                parsed.netloc,
                parsed.path,
                urlencode(new_pairs, doseq=True),
                parsed.fragment,
            )
        ) + trailing

    return full


def line_contains_preserved_domain(line: str, context: ScrubContext) -> bool:
    lowered = line.lower()
    return any(domain.lower() in lowered for domain in context.preserve_domains)


def redact_ipv4_contextual(text: str, high_network: bool, context: ScrubContext) -> str:
    rules = context.rules
    replacement = context.replacement

    redact_private_ips = bool(builtin_option(rules, "network", "redact_private_ips", True))
    redact_public_ip_ports = bool(
        builtin_option(rules, "network", "redact_public_ip_ports", True)
    )
    redact_public_high_network = bool(
        builtin_option(rules, "network", "redact_public_ips_in_high_network_logs", False)
    )
    redact_labeled_public_ips = bool(
        builtin_option(rules, "network", "redact_labeled_public_ips", False)
    )
    preserve_steam_infra = bool(
        builtin_option(rules, "network", "preserve_steam_infrastructure", True)
    )

    ipv4_pattern = re.compile(
        r"(?<![0-9.])"
        r"(?:(?:25[0-5]|2[0-4][0-9]|1?[0-9]{1,2})\.){3}"
        r"(?:25[0-5]|2[0-4][0-9]|1?[0-9]{1,2})"
        r"(?![0-9.])"
    )

    def repl(match: re.Match) -> str:
        start, end = match.span()
        ip_text = match.group(0)

        if host_is_safe_placeholder_ip(ip_text, rules):
            return ip_text

        try:
            parsed_ip = ipaddress.ip_address(ip_text)
        except ValueError:
            return ip_text

        before = text[max(0, start - 100):start]
        after = text[end:min(len(text), end + 50)]

        line_start = text.rfind("\n", 0, start) + 1
        line_end = text.find("\n", end)
        if line_end == -1:
            line_end = len(text)
        line = text[line_start:line_end]

        # False-positive protections.
        if re.search(
            r"(?:Version|version|depot|sniper|pressure-vessel|scripts)\s*[:=]?\s*$",
            before,
        ):
            return ip_text

        # Avoid certificate OIDs and dotted numeric chains.
        if start > 0 and text[start - 1] == ".":
            return ip_text
        if end < len(text) and text[end:end + 1] == ".":
            return ip_text

        if preserve_steam_infra and line_contains_preserved_domain(line, context):
            return ip_text

        if parsed_ip.is_private and redact_private_ips:
            return replacement

        if re.match(r":[0-9]{2,5}\b", after) and redact_public_ip_ports:
            return replacement

        labeled_ip = re.search(
            r"(?:ip|ipv4|remote_ip|local_ip|address|addr|host|server|endpoint|peer|cm|connection|remote|local)\s*[:=]?\s*$",
            before,
            re.I,
        )

        if labeled_ip and (parsed_ip.is_private or redact_labeled_public_ips):
            return replacement

        if high_network and redact_public_high_network:
            return replacement

        return ip_text

    text, count = ipv4_pattern.subn(repl, text)

    # Count conservatively after substitution by checking replacement count delta elsewhere.
    # More precise counting happens in the caller around the whole built-in pass.
    return text


def apply_builtin_redactions(
    text: str,
    filename: str,
    relative_path: str,
    context: ScrubContext,
) -> str:
    rules = context.rules
    replacement = context.replacement
    high_network = filename in context.high_network_files

    before_total = text.count(replacement)

    # Dynamic discoveries first, especially Steam account names.
    if builtin_enabled(rules, "steam_account_name_discovery", True):
        text = redact_discovered_values(text, context)

        # Positional SteamUI format, even if discovery failed.
        if bool(
            builtin_option(
                rules,
                "steam_account_name_discovery",
                "redact_positional_login_events",
                True,
            )
        ):
            pattern = re.compile(
                r"(\bOnLoginStateChange\s+)([A-Za-z0-9_.-]{3,64})(?=\s+[-0-9])"
            )

            def repl(match: re.Match) -> str:
                candidate = match.group(2)
                if valid_account_name(candidate, rules):
                    return match.group(1) + replacement
                return match.group(0)

            text, count = pattern.subn(repl, text)
            context.add_redactions("builtin_steamui_on_login_state_change", count)

    if builtin_enabled(rules, "identity", True):
        if bool(builtin_option(rules, "identity", "redact_emails", True)):
            text, count = re.subn(
                r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}",
                replacement,
                text,
            )
            context.add_redactions("builtin_email", count)

    if builtin_enabled(rules, "steam_ids", True):
        if bool(builtin_option(rules, "steam_ids", "redact_steamid64", True)):
            text, count = re.subn(r"\b7656119[0-9]{10}\b", replacement, text)
            context.add_redactions("builtin_steamid64", count)

        if bool(builtin_option(rules, "steam_ids", "redact_steam3_nonzero", True)):
            preserve_zero = bool(
                builtin_option(rules, "steam_ids", "preserve_steam3_zero", True)
            )
            if preserve_zero:
                pattern = r"\[U:1:(?!0\])[0-9]+\]"
            else:
                pattern = r"\[U:1:[0-9]+\]"

            text, count = re.subn(pattern, replacement, text)
            context.add_redactions("builtin_steam3", count)

    if builtin_enabled(rules, "credentials", True):
        if bool(builtin_option(rules, "credentials", "redact_ssfn", True)):
            text, count = re.subn(r"\bssfn[0-9]+\b", replacement, text)
            context.add_redactions("builtin_ssfn", count)

        sensitive_keys = get_nested(
            rules,
            "field_policy.sensitive_field_keys",
            DEFAULT_SENSITIVE_FIELD_KEYS,
        )
        sensitive_key_pattern = "|".join(re.escape(str(key)) for key in sensitive_keys)

        if sensitive_key_pattern:
            text, count = re.subn(
                rf"\b({sensitive_key_pattern})\b[A-Za-z0-9_.-]*\s*[:=]\s*[\"']?[^\"'\s,;<>]+",
                lambda match: match.group(1) + "=" + replacement,
                text,
                flags=re.I,
            )
            context.add_redactions("builtin_sensitive_fields", count)

            text, count = re.subn(
                rf"\"({sensitive_key_pattern})[A-Za-z0-9_.-]*\"\s*:\s*\"[^\"]+\"",
                lambda match: "\"" + match.group(1) + "\":\"" + replacement + "\"",
                text,
                flags=re.I,
            )
            context.add_redactions("builtin_sensitive_json_fields", count)

        if bool(builtin_option(rules, "credentials", "redact_bearer_basic_headers", True)):
            # Only redact actual Authorization-style headers/fields.
            # Do not redact harmless Chromium flags like:
            #   --password-store=basic --disable-quick-menu
            text, count = re.subn(
                r"((?:Authorization|Proxy-Authorization)\s*[:=]\s*)(Bearer|Basic)\s+[^\s,;<>]+",
                lambda match: match.group(1) + match.group(2) + " " + replacement,
                text,
                flags=re.I,
            )
            context.add_redactions("builtin_auth_header", count)

    if builtin_enabled(rules, "steam_ids", True):
        account_keys = get_nested(
            rules,
            "field_policy.account_field_keys",
            DEFAULT_ACCOUNT_FIELD_KEYS,
        )
        account_key_pattern = "|".join(re.escape(str(key)) for key in account_keys)

        preserve_steamid_zero = bool(
            builtin_option(rules, "steam_ids", "preserve_steamid_zero", True)
        )

        if account_key_pattern:
            if preserve_steamid_zero:
                value_pattern = r"(?!0\b)[^\"'\s,;<>]+"
            else:
                value_pattern = r"[^\"'\s,;<>]+"

            text, count = re.subn(
                rf"\b({account_key_pattern})\b\s*[:=]\s*[\"']?{value_pattern}",
                lambda match: match.group(1) + "=" + replacement,
                text,
                flags=re.I,
            )
            context.add_redactions("builtin_account_fields", count)

    if builtin_enabled(rules, "urls", True) or builtin_enabled(rules, "network", True):
        text, count = re.subn(
            r"https?://[^\s<>]+",
            lambda match: redact_url(match, high_network, context),
            text,
        )
        # This counts all URL matches, not only changed URLs. Correct it approximately.
        # The detailed total remains conservative and non-security-critical.
        context.add_redactions("builtin_url_pass", max(text.count(replacement) - before_total, 0))

    if builtin_enabled(rules, "network", True):
        before = text.count(replacement)
        text = redact_ipv4_contextual(text, high_network, context)
        context.add_redactions("builtin_ipv4_contextual", max(text.count(replacement) - before, 0))

        if bool(builtin_option(rules, "network", "redact_labeled_ipv6", True)):
            text, count = re.subn(
                r"\b(ipv6|ip6|remote_ip|local_ip|address|addr|host|server|endpoint|peer)\b\s*[:=]\s*\"?(?:[A-Fa-f0-9]{1,4}:){2,7}[A-Fa-f0-9]{1,4}",
                lambda match: match.group(1) + "=" + replacement,
                text,
                flags=re.I,
            )
            context.add_redactions("builtin_labeled_ipv6", count)

            text, count = re.subn(
                r"\[(?:[A-Fa-f0-9]{0,4}:){2,7}[A-Fa-f0-9]{0,4}\]",
                "[" + replacement + "]",
                text,
            )
            context.add_redactions("builtin_bracketed_ipv6", count)

    if builtin_enabled(rules, "hardware_identifiers", True):
        if bool(
            builtin_option(
                rules,
                "hardware_identifiers",
                "redact_mac_addresses",
                True,
            )
        ):
            text, count = re.subn(
                r"\b[0-9A-Fa-f]{2}([:-])[0-9A-Fa-f]{2}(?:\1[0-9A-Fa-f]{2}){4}\b",
                replacement,
                text,
            )
            context.add_redactions("builtin_mac_address", count)

        if bool(
            builtin_option(
                rules,
                "hardware_identifiers",
                "redact_labeled_private_guids",
                True,
            )
        ):
            private_guid_keys = get_nested(
                rules,
                "field_policy.private_guid_keys",
                DEFAULT_PRIVATE_GUID_KEYS,
            )
            private_guid_pattern = "|".join(re.escape(str(key)) for key in private_guid_keys)

            if private_guid_pattern:
                text, count = re.subn(
                    rf"\b({private_guid_pattern})\b\s*[:=]\s*\"?"
                    rf"[0-9a-fA-F]{{8}}-[0-9a-fA-F]{{4}}-[0-9a-fA-F]{{4}}-"
                    rf"[0-9a-fA-F]{{4}}-[0-9a-fA-F]{{12}}",
                    lambda match: match.group(1) + "=" + replacement,
                    text,
                    flags=re.I,
                )
                context.add_redactions("builtin_labeled_private_guid", count)

    if builtin_enabled(rules, "paths", False):
        if bool(builtin_option(rules, "paths", "redact_full_home_paths", False)):
            text, count = re.subn(
                r"/home/[^\s:;,)\"']+",
                "/home/" + replacement,
                text,
            )
            context.add_redactions("builtin_full_home_path", count)
        elif bool(builtin_option(rules, "paths", "redact_home_username", False)):
            text, count = re.subn(
                r"/home/([^/\s:;,)\"']+)",
                "/home/" + replacement,
                text,
            )
            context.add_redactions("builtin_home_username", count)

    text = re.sub(rf"\b{re.escape(replacement)}:[0-9]{{2,5}}\b", replacement, text)

    return text


def scrub_text(
    text: str,
    filename: str,
    relative_path: str,
    context: ScrubContext,
) -> str:
    text, preserve_map = protect_preserve_rules(text, filename, relative_path, context)

    text = apply_regex_rules_by_phase(
        text,
        filename,
        relative_path,
        context,
        {"before_builtin", "pre_builtin", "after_discovery"},
    )

    text = apply_builtin_redactions(text, filename, relative_path, context)

    text = apply_regex_rules_by_phase(
        text,
        filename,
        relative_path,
        context,
        {"main", "after_builtin", "post_builtin"},
    )

    text = restore_preserved_text(text, preserve_map)

    return text


def build_context(rules: dict[str, Any]) -> ScrubContext:
    replacement = str(get_nested(rules, "output.replacement", DEFAULT_REPLACEMENT))

    preserve_domains = get_nested(
        rules,
        "domain_policy.preserve_domains",
        DEFAULT_PRESERVE_DOMAINS,
    )

    redact_domains = get_nested(
        rules,
        "domain_policy.redact_domains",
        DEFAULT_REDACT_DOMAINS,
    )

    high_network_files = set(
        get_nested(rules, "file_classes.high_network", DEFAULT_HIGH_NETWORK_FILES)
    )

    return ScrubContext(
        rules=rules,
        replacement=replacement,
        preserve_domains=[str(domain).lower() for domain in preserve_domains],
        redact_domains=[str(domain).lower() for domain in redact_domains],
        high_network_files={str(name) for name in high_network_files},
    )


def scan_leftovers(output_dir: Path, context: ScrubContext) -> list[str]:
    replacement = re.escape(context.replacement)

    sensitive_field_keys = get_nested(
        context.rules,
        "field_policy.sensitive_field_keys",
        DEFAULT_SENSITIVE_FIELD_KEYS,
    )
    sensitive_field_pattern = "|".join(re.escape(str(key)) for key in sensitive_field_keys)

    patterns: list[tuple[str, re.Pattern]] = [
        (
            "email",
            re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"),
        ),
        (
            "steamid64",
            re.compile(r"\b7656119[0-9]{10}\b"),
        ),
        (
            "steam3_nonzero",
            re.compile(r"\[U:1:(?!0\])[0-9]+\]"),
        ),
        (
            "ssfn",
            re.compile(r"\bssfn[0-9]+\b"),
        ),
        (
            "url_ip_host",
            re.compile(
                r"https?://(?!(?:127\.|0\.0\.0\.0))"
                r"(?:25[0-5]|2[0-4][0-9]|1?[0-9]{1,2})"
                r"(?:\.(?:25[0-5]|2[0-4][0-9]|1?[0-9]{1,2})){3}"
            ),
        ),
        (
            "bare_ip_port",
            re.compile(
                r"(?<![0-9.])(?!(?:127\.|0\.0\.0\.0))"
                r"(?:(?:25[0-5]|2[0-4][0-9]|1?[0-9]{1,2})\.){3}"
                r"(?:25[0-5]|2[0-4][0-9]|1?[0-9]{1,2})"
                r":[0-9]{2,5}(?![0-9.])"
            ),
        ),
        (
            "mac_address",
            re.compile(r"\b[0-9A-Fa-f]{2}([:-])[0-9A-Fa-f]{2}(?:\1[0-9A-Fa-f]{2}){4}\b"),
        ),
        (
            "login_state_name",
            re.compile(
                rf"\bOnLoginStateChange\s+(?!{replacement}\b)([A-Za-z0-9_.-]{{3,64}})(?=\s+[-0-9])"
            ),
        ),
    ]

    if sensitive_field_pattern:
        patterns.append(
            (
                "sensitive_field",
                re.compile(rf"\b(?:{sensitive_field_pattern})\b\s*[:=]", re.I),
            )
        )

    for value_type, values in context.discovered_values.items():
        for value in values:
            if len(value) < 3:
                continue
            patterns.append(
                (
                    f"discovered_{value_type}",
                    re.compile(
                        rf"(?<![A-Za-z0-9_.-]){re.escape(value)}(?![A-Za-z0-9_.-])",
                        re.I,
                    ),
                )
            )

    leftovers: list[str] = []

    for path in output_dir.rglob("*"):
        if not path.is_file():
            continue

        try:
            data = path.read_bytes()
        except OSError:
            continue

        if is_binary(data):
            continue

        text = decode_text(data)
        relative = str(path.relative_to(output_dir))

        for label, pattern in patterns:
            if label in {"bare_ip_port", "url_ip_host"}:
                found = False
                for line in text.splitlines():
                    if pattern.search(line):
                        if line_contains_preserved_domain(line, context):
                            continue
                        found = True
                        break

                if found:
                    leftovers.append(f"{label}: {relative}")
                    break

                continue

            if pattern.search(text):
                leftovers.append(f"{label}: {relative}")
                break

    return leftovers


def scrub_folder(
    input_dir: str | Path,
    output_dir: str | Path,
    rules: dict[str, Any],
    dry_run: bool = False,
    force: bool = False,
) -> ScrubResult:
    input_path = Path(input_dir).expanduser().resolve()
    output_path = Path(output_dir).expanduser().resolve()

    if not input_path.is_dir():
        raise NotADirectoryError(f"Input folder does not exist: {input_path}")

    if input_path == output_path:
        raise ValueError("Refusing to scrub into the input folder.")

    context = build_context(rules)

    result = ScrubResult(
        input_dir=input_path,
        output_dir=output_path,
        rules_name=str(rules.get("profile_name", "unknown")),
        dry_run=dry_run,
    )

    discover_values(input_path, context)

    result.account_names_detected = len(context.discovered_values.get("steam_account_name", set()))
    result.discovered_value_counts = {
        key: len(values) for key, values in context.discovered_values.items()
    }

    if not dry_run:
        result.output_backup = backup_existing_output(output_path, force=force)
        output_path.mkdir(parents=True, exist_ok=True)

    for root, _, files in os.walk(input_path):
        root_path = Path(root)
        relative_root = root_path.relative_to(input_path)

        if not dry_run:
            out_root = output_path / relative_root
            out_root.mkdir(parents=True, exist_ok=True)

        for filename in files:
            in_file = root_path / filename
            relative_path = str(in_file.relative_to(input_path))

            result.files_scanned += 1

            try:
                data = in_file.read_bytes()
            except OSError as exc:
                result.warnings.append(f"Could not read {relative_path}: {exc}")
                continue

            if is_binary(data):
                result.binary_files += 1

                if not dry_run:
                    out_file = output_path / relative_path
                    out_file.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(in_file, out_file)

                continue

            result.text_files += 1
            original_text = decode_text(data)

            before_counts = dict(context.redaction_counts)
            scrubbed_text = scrub_text(original_text, filename, relative_path, context)

            changed = scrubbed_text != original_text

            if changed:
                result.files_changed += 1

            if not dry_run:
                out_file = output_path / relative_path
                out_file.parent.mkdir(parents=True, exist_ok=True)
                out_file.write_bytes(encode_text(scrubbed_text))
                shutil.copystat(in_file, out_file)

            # Per-file delta is not currently exposed, but keeping the before snapshot
            # here makes it easy to add later without changing the public API.
            _ = before_counts

    result.redaction_counts = dict(sorted(context.redaction_counts.items()))
    result.redactions = sum(context.redaction_counts.values())
    result.warnings.extend(context.warnings)

    if not dry_run:
        result.leftovers = scan_leftovers(output_path, context)
    else:
        # Dry runs do not write output. To avoid creating temp trees here, report only
        # known rule/config warnings and aggregate counts.
        result.leftovers = []

    return result
