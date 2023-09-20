from __future__ import annotations

import email
import multiprocessing
import os
import shutil
import tarfile
from abc import ABC, abstractmethod, abstractproperty
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
from packaging.utils import canonicalize_name, canonicalize_version
from pypi_simple import PyPISimple, tqdm_progress_factory

if TYPE_CHECKING:
    from forge.cross import CrossVEnv
    from forge.package import Package


class Builder(ABC):
    def __init__(self, cross_venv: CrossVEnv, package: Package):
        self.cross_venv = cross_venv
        self.package = package

    @abstractproperty
    def build_path(self) -> Path:
        ...

    @abstractproperty
    def source_file_path(self) -> Path:
        ...

    def install_host_requirements(self):
        pass

    def install_build_requirements(self):
        pass

    @abstractmethod
    def download_source(self):
        """Download the source tarball."""
        ...

    def unpack_source(self):
        if self.build_path.is_dir():
            print(f"Removing {self.build_path.relative_to(Path.cwd())}...")
            shutil.rmtree(self.build_path)

        print(f"Unpacking {self.source_file_path.relative_to(Path.cwd())}...")

        # This is the equivalent of --strip-components=<strip>
        def members(tf: tarfile.TarFile, strip=1):
            for member in tf.getmembers():
                parts = member.path.split("/", strip)
                try:
                    member.path = parts[strip]
                    yield member
                except IndexError:
                    pass

        with tarfile.open(self.source_file_path) as tar:
            tar.extractall(
                path=self.build_path,
                members=members(tar, strip=1),
            )

    def apply_patches(self):
        for patchfile in (self.package.recipe_path / "patches").glob("*.patch"):
            print(f"Applying {patchfile.relative_to(self.package.recipe_path)}...")
            self.cross_venv.run(
                ["patch", "-p1", "-i", str(patchfile)],
                cwd=self.build_path,
            )

    def prepare(self):
        self.install_host_requirements()
        self.install_build_requirements()

        if not self.source_file_path.is_file():
            print(f"\n[{self.cross_venv}] Download package sources")
            self.download_source()

        print(f"\n[{self.cross_venv}] Unpack sources")
        self.unpack_source()

        print(f"\n[{self.cross_venv}] Apply patches")
        self.apply_patches()

    @abstractmethod
    def build(self):
        """Build the package."""
        ...


class SimplePackageBuilder(Builder):
    """A builder for projects that have a build.sh entry point."""

    @property
    def source_file_path(self) -> Path:
        url = self.package.meta["source"]["url"]
        filename = url.split("/")[-1]
        return Path.cwd() / "downloads" / filename

    @property
    def build_path(self) -> Path:
        # Generate a separate build path for each platform, since we can't guarantee
        # that the Makefile will do a truly clean build for each platform.
        return (
            Path.cwd()
            / "build"
            / self.package.name
            / self.package.version
            / self.cross_venv.tag
        )

    def download_source(self):
        url = self.package.meta["source"]["url"]

        print(f"Downloading {url}...", end="", flush=True)
        self.source_file_path.parent.mkdir(parents=True, exist_ok=True)
        with httpx.stream("GET", url, follow_redirects=True) as response:
            with self.source_file_path.open("wb") as f:
                for chunk in response.iter_bytes():
                    print(".", end="", flush=True)
                    f.write(chunk)
        print(" done.")

    def write_message_file(self, filename, data):
        msg = email.message.Message()
        for key, value in data.items():
            msg[key] = value

        # I don't know whether maxheaderlen is required, but it's used by bdist_wheel.
        with filename.open("w", encoding="utf-8") as f:
            email.generator.Generator(f, maxheaderlen=0).flatten(msg)

    def make_wheel(self):
        build_num = str(self.package.meta["build"]["number"])
        name = canonicalize_name(self.package.name)
        version = canonicalize_version(self.package.version)
        info_path = self.build_path / "wheel" / f"{name}-{version}.dist-info"
        info_path.mkdir()

        # Write the packaging metadata
        self.write_message_file(
            info_path / "WHEEL",
            {
                "Wheel-Version": "1.0",
                "Root-Is-Purelib": "false",
                "Generator": "mobile-forge",
                "Build": build_num,
                "Tag": f"py3-none-{self.cross_venv.tag}",
            },
        )
        self.write_message_file(
            info_path / "METADATA",
            {
                "Metadata-Version": "1.2",
                "Name": self.package.name,
                "Version": self.package.version,
                "Summary": "",  # Compulsory according to PEP 345,
                "Download-URL": "",
            },
        )

        # Re-pack the wheel file
        self.cross_venv.run(
            [
                "build-python",
                "-m",
                "wheel",
                "pack",
                str(self.build_path / "wheel"),
                "--dest-dir",
                str(Path.cwd() / "dist"),
                "--build-number",
                str(build_num),
            ]
        )

    def compile(self):
        sysconfig_data = self.cross_venv.sysconfig_data
        install_root = self.cross_venv.install_root

        cc = sysconfig_data["CC"]

        cflags = self.cross_venv.sysconfig_data["CFLAGS"]
        cflags += f" -I{install_root}/include"

        ldflags = self.cross_venv.sysconfig_data["LDFLAGS"]
        ldflags += f" -I{install_root}/lib"

        self.cross_venv.run(
            [
                str(self.package.recipe_path / "build.sh"),
            ],
            cwd=self.build_path,
            env={
                "HOST_TRIPLET": self.cross_venv.platform_triplet,
                "BUILD_TRIPLET": f"{os.uname().machine}-apple-darwin",
                "CPU_COUNT": str(multiprocessing.cpu_count()),
                "PREFIX": str(self.build_path / "wheel" / "opt"),
                "CC": cc,
                "CFLAGS": cflags,
                "LDFLAGS": ldflags,
            },
        )

    def build(self):
        self.compile()
        self.make_wheel()


class CMakePackageBuilder(SimplePackageBuilder):
    """A builder for cmake-based projects."""

    def build(self):
        pass


class PythonPackageBuilder(Builder):
    """A builder for projects available on PyPI."""

    @property
    def source_file_path(self) -> Path:
        return (
            Path.cwd()
            / "downloads"
            / f"{self.package.name}-{self.package.version}.tar.gz"
        )

    @property
    def build_path(self) -> Path:
        return Path.cwd() / "build" / self.package.name / self.package.version

    def download_source(self):
        with PyPISimple() as client:
            page = client.get_project_page(self.package.name)
            sdists = [
                package
                for package in page.packages
                if package.package_type == "sdist"
                and package.version == self.package.version
            ]

            client.download_package(
                sdists[0],
                path=self.source_file_path,
                progress=tqdm_progress_factory(),
            )

    def build(self):
        self.cross_venv.run(
            [
                "python",
                "-m",
                "build",
                "--no-isolation",
                "--wheel",
                "--outdir",
                str(Path.cwd() / "dist"),
            ],
            cwd=self.build_path,
        )
