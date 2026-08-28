"""Build and inspect an Ideogram 4 prompt without running image generation."""

from __future__ import annotations

import argparse
from pathlib import Path

from backend.core import load_config
from backend.models.ideogram_prompt import (
    IdeogramPromptError,
    build_ideogram_v4_prompt,
    preview_ideogram_prompt,
)
from backend.models.registry import BackendRegistry


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prompt", nargs="?", help="Natural-language prompt for Quick mode")
    parser.add_argument(
        "--prompt-mode", choices=("quick", "precise"), default="quick",
        help="Quick expands natural language; Precise validates native JSON (default: quick)",
    )
    parser.add_argument(
        "--prompt-json", type=Path,
        help="Native Ideogram/KJNodes JSON file required by Precise mode",
    )
    parser.add_argument(
        "--aspect-ratio", default="1:1",
        help="Quick-mode target ratio in W:H form (default: 1:1)",
    )
    parser.add_argument(
        "--show-prompt-json", action="store_true",
        help="Print the exact compact JSON that will be sent to Ideogram",
    )
    parser.add_argument(
        "--save-prompt-json", type=Path,
        help="Save the exact compact canonical caption",
    )
    arguments = parser.parse_args()

    if arguments.prompt_mode == "quick" and not arguments.prompt:
        parser.error("Quick mode requires a natural-language prompt")
    if arguments.prompt_mode == "precise" and arguments.prompt_json is None:
        parser.error("Precise mode requires --prompt-json PATH")

    precise_json = None
    llm = None
    if arguments.prompt_json is not None:
        try:
            precise_json = arguments.prompt_json.read_text(encoding="utf-8")
        except OSError as exc:
            parser.error(f"cannot read --prompt-json: {exc}")
    if arguments.prompt_mode == "quick":
        config = load_config()
        registry = BackendRegistry.from_config(config.model_dump(mode="python"))
        llm = registry.get("local_llm")

    try:
        result = build_ideogram_v4_prompt(
            arguments.prompt,
            mode=arguments.prompt_mode,
            aspect_ratio=arguments.aspect_ratio,
            precise_json=precise_json,
            llm=llm,
        )
    except IdeogramPromptError as exc:
        parser.error(str(exc))

    print(preview_ideogram_prompt(
        result["structured_prompt"],
        mode=result["mode"],
        aspect_ratio=arguments.aspect_ratio if result["mode"] == "quick" else None,
    ))
    if result["warnings"]:
        print("\nWARNINGS")
        for warning in result["warnings"]:
            print(f"- {warning}")
    if arguments.show_prompt_json:
        print(f"\nPROMPT JSON\n{result['serialized_prompt']}")
    if arguments.save_prompt_json is not None:
        arguments.save_prompt_json.parent.mkdir(parents=True, exist_ok=True)
        arguments.save_prompt_json.write_text(
            result["serialized_prompt"] + "\n", encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
