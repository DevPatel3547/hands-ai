"""Run the live Claude code path end to end with no key and no network.

Starts the local fake Messages API (tests/fakes/anthropic_server.py, which answers like the
scripted model but over the real wire format: streaming, tool use) and runs
``hands demo <gadget> --sim --model anthropic`` against it through the real `anthropic` SDK.

    python scripts/demo_live_fake.py [steelers|tempdisplay|servo]
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hands.demo import run_sim_demo
from tests.fakes.anthropic_server import FakeAnthropic, gadget_responder


def main() -> int:
    gadget = sys.argv[1] if len(sys.argv) > 1 else "steelers"
    fake = FakeAnthropic(gadget_responder(gadget), api_key="fake-key-for-local-server")
    url = fake.start()
    os.environ["ANTHROPIC_API_KEY"] = "fake-key-for-local-server"
    print(f"#### FAKE Anthropic API at {url}: canned scripted replies over the real wire format, not Claude\n")
    try:
        res = run_sim_demo(gadget, model="anthropic", base_url=url, metrics_path=None)
    finally:
        fake.stop()
    print(f"\n#### {len(fake.requests)} Messages API requests served by the fake (streamed, tool use)")
    return 0 if res.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
