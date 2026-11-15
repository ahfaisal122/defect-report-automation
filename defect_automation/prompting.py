"""Load a versioned prompt file and fill in the allowed values and the report text.

The prompt version is the file name without its extension. It is stored with every
extraction, so any answer can be traced back to the exact prompt that produced it.
"""

from pathlib import Path

from defect_automation.domain import DEFECT_TYPE_DESCRIPTIONS, SEVERITY_RUBRIC


def load_template(prompts_dir: Path, version: str) -> str:
    return (prompts_dir / f"{version}.md").read_text(encoding="utf-8")


def _neutralise_delimiters(report_text: str) -> str:
    """Report text must not be able to close the data block and start giving orders."""
    return report_text.replace("<report>", "(report)").replace("</report>", "(/report)")


def render_prompt(template: str, stations: dict[str, str], report_text: str) -> str:
    values = {
        "{{defect_types}}": "\n".join(
            f"- {kind.value}: {text}" for kind, text in DEFECT_TYPE_DESCRIPTIONS.items()
        ),
        "{{severities}}": "\n".join(
            f"- {level.value}: {text}" for level, text in SEVERITY_RUBRIC.items()
        ),
        "{{stations}}": "\n".join(f"- {code}: {name}" for code, name in stations.items()),
    }
    for placeholder, value in values.items():
        template = template.replace(placeholder, value)
    # The report goes in last, so placeholders inside report text are never expanded.
    return template.replace("{{report}}", _neutralise_delimiters(report_text))
