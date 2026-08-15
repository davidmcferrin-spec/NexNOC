"""
prometheus_util.py - Minimal Prometheus text exposition parser (stdlib).

Confirmed against live Appear X20 scrapes (DC frame) at
/prometheus/{system,product,ipgateway,alarms}/metrics.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class PromSample:
    name: str
    labels: dict[str, str]
    value: float


def looks_like_prometheus(body: str) -> bool:
    if not body:
        return False
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("# HELP ") or stripped.startswith("# TYPE "):
            return True
        if stripped and not stripped.startswith("#") and "{" in stripped:
            return True
    return False


def parse_prometheus(body: str) -> list[PromSample]:
    samples: list[PromSample] = []
    for raw in body.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            samples.append(_parse_line(line))
        except ValueError:
            continue
    return samples


def _parse_line(line: str) -> PromSample:
    if "{" in line:
        name, rest = line.split("{", 1)
        labels_blob, value_blob = rest.rsplit("}", 1)
        labels = _parse_labels(labels_blob)
    else:
        parts = line.split(None, 1)
        if len(parts) != 2:
            raise ValueError("no value")
        name, value_blob = parts
        labels = {}
    value_blob = value_blob.strip().split()[0]
    return PromSample(name=name.strip(), labels=labels, value=float(value_blob))


def _parse_labels(blob: str) -> dict[str, str]:
    labels: dict[str, str] = {}
    token = ""
    key = ""
    in_quote = False
    escape = False
    i = 0
    while i < len(blob):
        ch = blob[i]
        if in_quote:
            if escape:
                token += ch
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_quote = False
                labels[key] = token
                token = ""
                key = ""
            else:
                token += ch
        else:
            if ch == '"':
                in_quote = True
                token = ""
            elif ch == "=":
                key = token.strip().lstrip(",")
                token = ""
            else:
                token += ch
        i += 1
    return labels


def label(sample: PromSample, key: str, default: str = "") -> str:
    return sample.labels.get(key, default)


def sum_named(samples: list[PromSample], name: str, **match: str) -> float:
    total = 0.0
    for s in samples:
        if s.name != name:
            continue
        if any(s.labels.get(k) != v for k, v in match.items()):
            continue
        total += s.value
    return total


def first_value(samples: list[PromSample], name: str) -> Optional[float]:
    for s in samples:
        if s.name == name:
            return s.value
    return None
