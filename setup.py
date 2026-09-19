"""Copy the canonical Library assets into distributable wheels."""
from pathlib import Path
from shutil import copytree

from setuptools import setup
from setuptools.command.build_py import build_py


class BuildWithLibrary(build_py):
    def run(self):
        super().run()
        source = Path(__file__).parent / "skills"
        if not (source / "index.json").is_file():
            raise RuntimeError("Atlas Library index missing from build source")
        copytree(source, Path(self.build_lib) / "cms" / "builtin_skills", dirs_exist_ok=True)


setup(cmdclass={"build_py": BuildWithLibrary})
