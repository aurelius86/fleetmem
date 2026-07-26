#!/usr/bin/env python3
"""Seam test (audit inv-autolearn / cloud-routing): autolearn extraction NEVER sends
sensitive/secret data to a cloud backend.

Proves pick_backend() routes sensitivity in {sensitive, secret} to the LOCAL backend for
EVERY allow_cloud value, and that a cloud backend is only ever selected for public/normal
with allow_cloud=True and a cloud backend present. No infra needed — pure unit test.

    cd db && python3 test_pick_backend.py     # exit 0 = PASS, 1 = FAIL
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # db/ on path -> autolearn package
from autolearn.extract import (pick_backend, LocalBackend, CloudBackend,  # noqa: E402
                               extract_session, has_sensitive_topic, SensitiveTopicToCloud)

LOCAL = LocalBackend()
CLOUD = CloudBackend()

# case + whitespace variants guard against a future .lower()/strip() regression
SENSITIVITIES = ["public", "normal", "sensitive", "secret", "SENSITIVE", "Secret"]
CLOUD_OPTS = [CLOUD, None]
ALLOW_OPTS = [True, False]


def _t569(fails):
    """private TOPICS (medical/health/financial) must not reach a cloud extractor, even when the
    DECLARED sensitivity is normal/public (it's coarse at extract time; scrub only redacts secret shapes)."""
    # topic overrides a NORMAL declared sensitivity -> LOCAL, even with allow_cloud + a cloud backend
    if pick_backend("normal", LOCAL, cloud=CLOUD, allow_cloud=True, text="the patient's prescription") is not LOCAL:
        fails.append("private-topic text did not force LOCAL in pick_backend")
    # benign text still permits cloud (no false positive that would neuter cloud extraction entirely)
    if pick_backend("normal", LOCAL, cloud=CLOUD, allow_cloud=True, text="restart nginx after deploy") is not CLOUD:
        fails.append("benign text wrongly blocked cloud in pick_backend")
    if not has_sensitive_topic("the quarterly financial report"):
        fails.append("has_sensitive_topic missed 'financial'")
    if has_sensitive_topic("restart the gunicorn workers"):
        fails.append("has_sensitive_topic false-positive on benign text")

    # extract_session REFUSES a cloud backend for private-topic spans (fail-closed, before generate)
    class _CloudStub:
        name = "cloud"
        def generate(self, prompt, system=None):
            raise AssertionError("generate must NOT be reached for private-topic spans")
    raised = False
    try:
        extract_session([{"channel": "human-input", "text": "the patient's diagnosis and prescription"}], _CloudStub())
    except SensitiveTopicToCloud:
        raised = True
    if not raised:
        fails.append("extract_session did not refuse cloud for private-topic spans")

    # extract_session does NOT fire the topic guard on benign spans (guard is topic-specific)
    class _CloudOK:
        name = "cloud"
        def generate(self, prompt, system=None):
            return "[]"
    try:
        extract_session([{"channel": "human-input", "text": "restart nginx after deploy"}], _CloudOK())
    except SensitiveTopicToCloud:
        fails.append("topic guard wrongly fired on benign spans")
    except Exception:
        pass  # any OTHER downstream error is out of scope for this guard-focused test


def main():
    fails = []
    cloud_returns = []
    for sens in SENSITIVITIES:
        for cloud in CLOUD_OPTS:
            for allow in ALLOW_OPTS:
                got = pick_backend(sens, LOCAL, cloud=cloud, allow_cloud=allow)
                is_cloud = got is CLOUD
                low = str(sens).lower()
                if low in ("sensitive", "secret") and is_cloud:
                    fails.append(f"LEAK sens={sens!r} allow={allow} "
                                 f"cloud={'set' if cloud else 'None'} -> CLOUD")
                expect_cloud = (low not in ("sensitive", "secret")) and allow and (cloud is not None)
                if is_cloud != expect_cloud:
                    fails.append(f"MISMATCH sens={sens!r} allow={allow} "
                                 f"cloud={'set' if cloud else 'None'} -> "
                                 f"{'CLOUD' if is_cloud else got.name}, "
                                 f"expected {'CLOUD' if expect_cloud else 'local'}")
                if is_cloud:
                    cloud_returns.append((low, allow))

    raised = False
    try:
        pick_backend("public", None, cloud=CLOUD, allow_cloud=True)
    except ValueError:
        raised = True
    if not raised:
        fails.append("MISSING GUARD: local=None did not raise ValueError")

    _t569(fails)

    n = len(SENSITIVITIES) * len(CLOUD_OPTS) * len(ALLOW_OPTS)
    print(f"cases: {n} + 1 guard | cloud returned only for: {sorted(set(cloud_returns))}")
    if fails:
        print("FAIL (%d):" % len(fails))
        for f in fails:
            print("  -", f)
        return 1
    print("PASS: sensitive/secret never -> cloud; cloud only for public/normal+allow_cloud+cloud; "
          "local=None guarded.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
