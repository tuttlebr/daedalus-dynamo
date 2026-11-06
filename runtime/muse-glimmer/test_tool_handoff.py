#!/usr/bin/env python3
"""Replay ATEM output through the installed parsers; uses CPU and cached tokenizer only."""

from __future__ import annotations

import argparse
import json
from types import SimpleNamespace
import unittest

from transformers import AutoTokenizer
from vllm.entrypoints.openai.chat_completion.protocol import ChatCompletionRequest
from vllm.reasoning.muse_glimmer_reasoning_parser import MuseGlimmerReasoningParser
from vllm.sampling_params import SamplingParams
from vllm.tool_parsers.muse_glimmer_tool_parser import MuseGlimmerToolParser

from dynamo.frontend.prepost import StreamingPostProcessor


TOOL = {
    "type": "function",
    "function": {
        "name": "probe_read",
        "parameters": {
            "type": "object", "properties": {"key": {"type": "string"}},
            "required": ["key"], "additionalProperties": False,
        },
    },
}
INVOKE = '<atem:invoke name="probe_read"><atem:parameter name="key">probe</atem:parameter></atem:invoke>'
REASONING = " to=self<|message|>I need to call the tool.<|eom|>"
CHANNEL = "<|start|>assistant to=probe_read<|message|><atem:function_calls>"
END = "</atem:function_calls><|eom|>"


class ToolHandoffTests(unittest.TestCase):
    tokenizer = None

    def test_tools_across_chunk_boundaries_and_choices(self) -> None:
        for count in (1, 2):
            text = REASONING + CHANNEL + INVOKE * count + END
            ids = self.tokenizer.encode(text, add_special_tokens=False)
            for stream in (True, False):
                for stride in (1, 15, len(ids)):
                    for choice in ("auto", "required", {"type": "function", "function": {"name": "probe_read"}}):
                        with self.subTest(count=count, stream=stream, stride=stride, choice=choice):
                            request = ChatCompletionRequest(
                                model="muse", messages=[{"role": "user", "content": "call probe_read"}],
                                tools=[TOOL], tool_choice=choice, stream=stream,
                            )
                            post = StreamingPostProcessor(
                                tokenizer=self.tokenizer, request_for_sampling=request,
                                sampling_params=SamplingParams(),
                                prompt_token_ids=self.tokenizer.encode("<|start|>assistant", add_special_tokens=False),
                                tool_parser=MuseGlimmerToolParser(self.tokenizer, request.tools),
                                reasoning_parser_class=MuseGlimmerReasoningParser,
                                chat_template_kwargs={}, stream_response=stream,
                            )
                            choices = []
                            previous = ""
                            for offset in range(0, len(ids), stride):
                                end = min(offset + stride, len(ids))
                                decoded = self.tokenizer.decode(ids[:end], skip_special_tokens=False)
                                output = SimpleNamespace(
                                    index=0, text=decoded[len(previous):], token_ids=ids[offset:end],
                                    finish_reason="stop" if end == len(ids) else None, logprobs=None,
                                )
                                result = post.process_output(output)
                                if result:
                                    choices.append(result)
                                previous = decoded
                            calls = [call for item in choices for call in item["delta"].get("tool_calls", [])]
                            content = "".join(item["delta"].get("content", "") for item in choices)
                            self.assertEqual(len(calls), count)
                            self.assertEqual(len({call["id"] for call in calls}), count)
                            self.assertEqual(content, "")
                            self.assertEqual(choices[-1]["finish_reason"], "tool_calls")
                            for call in calls:
                                self.assertEqual(call["function"]["name"], "probe_read")
                                self.assertEqual(json.loads(call["function"]["arguments"]), {"key": "probe"})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tokenizer", help="Local cached Muse tokenizer directory")
    args = parser.parse_args()
    ToolHandoffTests.tokenizer = AutoTokenizer.from_pretrained(
        args.tokenizer, local_files_only=True, trust_remote_code=True,
    )
    unittest.main(argv=[__file__], verbosity=2)
