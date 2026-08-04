from __future__ import annotations

from sample_helper import SAMPLE_VALUE


def create_application(context):
    return {
        "sample": SAMPLE_VALUE,
        "context": context,
    }
