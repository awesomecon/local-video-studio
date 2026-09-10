"""Export only JUnit identities, outcomes and timings, never captured payloads."""
from pathlib import Path
import xml.etree.ElementTree as ET


def sanitize(source: Path, destination: Path) -> None:
    root = ET.parse(source).getroot()
    allowed = {"name", "classname", "tests", "failures", "errors", "skipped", "time"}
    for element in root.iter():
        element.attrib = {key: value for key, value in element.attrib.items() if key in allowed}
        element.text = None
        element.tail = None
        for child in list(element):
            if child.tag not in {"testsuite", "testcase", "failure", "error", "skipped"}:
                element.remove(child)
    destination.parent.mkdir(exist_ok=True)
    ET.ElementTree(root).write(destination, encoding="utf-8", xml_declaration=True)


if __name__ == "__main__":
    source = Path("ci-raw-pytest.xml")
    if source.exists():
        sanitize(source, Path("ci-reports/pytest.xml"))
