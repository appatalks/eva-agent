#!/usr/bin/env python3
"""Contract test for strict LM Studio system-message ordering."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
from bridge.core import _lmstudio_camera_request, _lmstudio_chat_messages


def main():
    messages = _lmstudio_chat_messages(
        "Eva runtime instructions",
        [
            {"role": "system", "content": "Browser prompt instructions"},
            {"role": "developer", "content": "Runtime state"},
            {"role": "user", "content": "Hi Eva"},
            {"role": "user", "content": [
                {"type": "text", "text": "Look at this"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,ignored"}},
            ]},
        ],
        "Look at this",
        ["Camera reminder"],
    )
    assert [item["role"] for item in messages] == ["system", "user", "user"]
    assert messages[0]["content"] == "Eva runtime instructions\n\nBrowser prompt instructions\n\nRuntime state\n\nCamera reminder"
    assert messages[-1]["content"] == "Look at this"
    assert "base64" not in "\n".join(item["content"] for item in messages)
    assert not _lmstudio_camera_request("Look up the news today")
    assert not _lmstudio_camera_request("Look at what I am holding")
    assert not _lmstudio_camera_request("What can you see?")
    assert not _lmstudio_camera_request("Do not use the camera")
    assert not _lmstudio_camera_request("What is a camera shutter?")
    assert _lmstudio_camera_request("What do you see through the webcam?")
    print("LM Studio message ordering tests: PASS")


if __name__ == "__main__":
    main()
