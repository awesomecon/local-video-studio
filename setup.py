"""Build runtime resources without copying local configuration or development files."""

from pathlib import Path
import shutil

from setuptools import setup
from setuptools.command.build_py import build_py


class BuildRuntimeResources(build_py):
    def run(self):
        super().run()
        root = Path(__file__).parent
        destination = Path(self.build_lib) / "backend" / "_resources"
        # An allowlist prevents machine-local configuration and test artifacts
        # from entering either a wheel or a repeated build's output.
        if destination.exists():
            shutil.rmtree(destination)
        resources = [root / "config" / "default.yaml"]
        resources.extend(root.glob("workflows/**/*.json"))
        for pattern in ("index.html", "config.json", "js/**/*.js", "css/**/*.css", "assets/**/*.svg"):
            resources.extend((root / "frontend").glob(pattern))
        for source in resources:
            target = destination / source.relative_to(root)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)


setup(cmdclass={"build_py": BuildRuntimeResources})
