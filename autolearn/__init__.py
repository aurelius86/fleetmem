"""Brain-v2 auto-learn pipeline (Big Step 6).

Phase A (here): deterministic provenance — turn a session transcript into
source-tagged spans and derive a content-origin trust verdict. Pure stdlib,
no LLM, no DB writes. Later phases (B extract / C auto-keep+merge / D lessons
+ red-team) build on this foundation.
"""
