# Copyright (c) 2014-present PlatformIO <contact@platformio.org>
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# pylint: disable=no-self-use, unused-argument, too-many-lines
# pylint: disable=too-many-instance-attributes, too-many-public-methods
# pylint: disable=assignment-from-no-return

from __future__ import absolute_import

import hashlib
import io
import os
import re
import sys

import click
import SCons.Scanner  # pylint: disable=import-error
from SCons.Script import ARGUMENTS  # pylint: disable=import-error
from SCons.Script import COMMAND_LINE_TARGETS  # pylint: disable=import-error
from SCons.Script import DefaultEnvironment  # pylint: disable=import-error

from platformio import exception, fs, util
from platformio.builder.tools import platformio as piotool
from platformio.clients.http import HTTPClientError, InternetIsOffline
from platformio.compat import IS_WINDOWS, hashlib_encode_data, string_types
from platformio.package.exception import UnknownPackageError
from platformio.package.manager.library import LibraryPackageManager
from platformio.package.manifest.parser import (
    ManifestParserError,
    ManifestParserFactory,
)
from platformio.package.meta import PackageItem
from platformio.project.options import ProjectOptions


class LibBuilderFactory(object):
    @staticmethod
    def new(env, path, verbose=int(ARGUMENTS.get("PIOVERBOSE", 0))):
        clsname = "UnknownLibBuilder"
        if os.path.isfile(os.path.join(path, "library.json")):
            clsname = "PlatformIOLibBuilder"
        else:
            used_frameworks = LibBuilderFactory.get_used_frameworks(env, path)
            common_frameworks = set(env.get("PIOFRAMEWORK", [])) & set(used_frameworks)
            if common_frameworks:
                clsname = "%sLibBuilder" % list(common_frameworks)[0].title()
            elif used_frameworks:
                clsname = "%sLibBuilder" % used_frameworks[0].title()

        obj = getattr(sys.modules[__name__], clsname)(env, path, verbose=verbose)

        # Handle PlatformIOLibBuilder.manifest.build.builder
        # pylint: disable=protected-access
        if isinstance(obj, PlatformIOLibBuilder) and obj._manifest.get("build", {}).get(
            "builder"
        ):
            obj = getattr(
                sys.modules[__name__], obj._manifest.get("build", {}).get("builder")
            )(env, path, verbose=verbose)

        assert isinstance(obj, LibBuilderBase)
        return obj

    @staticmethod
    def get_used_frameworks(env, path):
        if any(
            os.path.isfile(os.path.join(path, fname))
            for fname in ("library.properties", "keywords.txt")
        ):
            return ["arduino"]

        if os.path.isfile(os.path.join(path, "module.json")):
            return ["mbed"]

        include_re = re.compile(
            r'^#include\s+(<|")(Arduino|mbed)\.h(<|")', flags=re.MULTILINE
        )

        # check source files
        for root, _, files in os.walk(path, followlinks=True):
            if "mbed_lib.json" in files:
                return ["mbed"]
            for fname in files:
                if not fs.path_endswith_ext(
                    fname, piotool.SRC_BUILD_EXT + piotool.SRC_HEADER_EXT
                ):
                    continue
                with io.open(
                    os.path.join(root, fname), encoding="utf8", errors="ignore"
                ) as fp:
                    content = fp.read()
                if not content:
                    continue
                if "Arduino.h" in content and include_re.search(content):
                    return ["arduino"]
                if "mbed.h" in content and include_re.search(content):
                    return ["mbed"]
        return []


class LibBuilderBase(object):

    CLASSIC_SCANNER = SCons.Scanner.C.CScanner()
    CCONDITIONAL_SCANNER = SCons.Scanner.C.CConditionalScanner()
    # Max depth of nested includes:
    # -1 = unlimited
    # 0 - disabled nesting
    # >0 - number of allowed nested includes
    CCONDITIONAL_SCANNER_DEPTH = 99
    PARSE_SRC_BY_H_NAME = True

    _INCLUDE_DIRS_CACHE = None

    def __init__(self, env, path, manifest=None, verbose=False):
        self.env = env.Clone()
        self.envorigin = env.Clone()
        self.path = os.path.abspath(env.subst(path))
        self.verbose = verbose

        try:
            self._manifest = manifest if manifest else self.load_manifest()
        except ManifestParserError:
            click.secho(
                "Warning! Ignoring broken library manifest in " + self.path, fg="yellow"
            )
            self._manifest = {}

        self._is_dependent = False
        self._is_built = False
        self._depbuilders = []
        self._circular_deps = []
        self._processed_files = []

        # reset source filter, could be overridden with extra script
        self.env["SRC_FILTER"] = ""

        # process extra options and append to build environment
        self.process_extra_options()

    def __repr__(self):
        return "%s(%r)" % (self.__class__, self.path)

    def __contains__(self, path):
        p1 = self.path
        p2 = path
        if IS_WINDOWS:
            p1 = p1.lower()
            p2 = p2.lower()
        if p1 == p2:
            return True
        return os.path.commonprefix((p1 + os.path.sep, p2)) == p1 + os.path.sep

    @property
    def name(self):
        return self._manifest.get("name", os.path.basename(self.path))

    @property
    def version(self):
        return self._manifest.get("version")

    @property
    def dependencies(self):
        return self._manifest.get("dependencies")

    @property
    def src_filter(self):
        return piotool.SRC_FILTER_DEFAULT + [
            "-<example%s>" % os.sep,
            "-<examples%s>" % os.sep,
            "-<test%s>" % os.sep,
            "-<tests%s>" % os.sep,
        ]

    @property
    def include_dir(self):
        for name in ("include", "Include"):
            d = os.path.join(self.path, name)
            if os.path.isdir(d):
                return d
        return None

    @property
    def src_dir(self):
        for name in ("src", "Src"):
            d = os.path.join(self.path, name)
            if os.path.isdir(d):
                return d
        return self.path

    def get_include_dirs(self):
        items = []
        include_dir = self.include_dir
        if include_dir:
            items.append(include_dir)
        items.append(self.src_dir)
        return items

    @property
    def build_dir(self):
        lib_hash = hashlib.sha1(hashlib_encode_data(self.path)).hexdigest()[:3]
        return os.path.join(
            "$BUILD_DIR", "lib%s" % lib_hash, os.path.basename(self.path)
        )

    @property
    def build_flags(self):
        return None

    @property
    def build_unflags(self):
        return None

    @property
    def extra_script(self):
        return None

    @property
    def depbuilders(self):
        return self._depbuilders

    @property
    def dependent(self):
        return self._is_dependent

    @property
    def is_built(self):
        return self._is_built

    @property
    def lib_archive(self):
        return self.env.GetProjectOption("lib_archive")

    @property
    def lib_ldf_mode(self):
        return self.env.GetProjectOption("lib_ldf_mode")

    @staticmethod
    def validate_ldf_mode(mode):
        ldf_modes = ProjectOptions["env.lib_ldf_mode"].type.choices
        if isinstance(mode, string_types):
            mode = mode.strip().lower()
        if mode in ldf_modes:
            return mode
        try:
            return ldf_modes[int(mode)]
        except (IndexError, ValueError):
            pass
        return ProjectOptions["env.lib_ldf_mode"].default

    @property
    def lib_compat_mode(self):
        return self.env.GetProjectOption("lib_compat_mode")

    @staticmethod
    def validate_compat_mode(mode):
        compat_modes = ProjectOptions["env.lib_compat_mode"].type.choices
        if isinstance(mode, string_types):
            mode = mode.strip().lower()
        if mode in compat_modes:
            return mode
        try:
            return compat_modes[int(mode)]
        except (IndexError, ValueError):
            pass
        return ProjectOptions["env.lib_compat_mode"].default

    def is_platforms_compatible(self, platforms):
        return True

    def is_frameworks_compatible(self, frameworks):
        return True

    def load_manifest(self):
        return {}

    def process_extra_options(self):
        self.env.ProcessFlags(self.build_flags)
        self.env.ProcessUnFlags(self.build_unflags)

    def process_dependencies(self):
        if not self.dependencies:
            return
        for item in self.dependencies:
            found = False
            for lb in self.env.GetLibBuilders():
                if item["name"] != lb.name:
                    continue
                found = True
                if lb not in self.depbuilders:
                    self.depend_recursive(lb)
                break

            if not found and self.verbose:
                sys.stderr.write(
                    "Warning: Ignored `%s` dependency for `%s` "
                    "library\n" % (item["name"], self.name)
                )

    def get_search_files(self):
        items = [
            os.path.join(self.src_dir, item)
            for item in self.env.MatchSourceFiles(self.src_dir, self.src_filter)
        ]
        include_dir = self.include_dir
        if include_dir:
            items.extend(
                [
                    os.path.join(include_dir, item)
                    for item in self.env.MatchSourceFiles(include_dir)
                ]
            )
        return items

    def _get_found_includes(  # pylint: disable=too-many-branches
        self, search_files=None
    ):
        # all include directories
        if not LibBuilderBase._INCLUDE_DIRS_CACHE:
            LibBuilderBase._INCLUDE_DIRS_CACHE = [
                self.env.Dir(d)
                for d in ProjectAsLibBuilder(
                    self.envorigin, "$PROJECT_DIR"
                ).get_include_dirs()
            ]
            for lb in self.env.GetLibBuilders():
                LibBuilderBase._INCLUDE_DIRS_CACHE.extend(
                    [self.env.Dir(d) for d in lb.get_include_dirs()]
                )

        # append self include directories
        include_dirs = [self.env.Dir(d) for d in self.get_include_dirs()]
        include_dirs.extend(LibBuilderBase._INCLUDE_DIRS_CACHE)

        result = []
        for path in search_files or []:
            if path in self._processed_files:
                continue
            self._processed_files.append(path)

            try:
                assert "+" in self.lib_ldf_mode
                candidates = LibBuilderBase.CCONDITIONAL_SCANNER(
                    self.env.File(path),
                    self.env,
                    tuple(include_dirs),
                    depth=self.CCONDITIONAL_SCANNER_DEPTH,
                )
                # mark candidates already processed via Conditional Scanner
                self._processed_files.extend(
                    [
                        c.get_abspath()
                        for c in candidates
                        if c.get_abspath() not in self._processed_files
                    ]
                )
            except Exception as e:  # pylint: disable=broad-except
                if self.verbose and "+" in self.lib_ldf_mode:
                    sys.stderr.write(
                        "Warning! Classic Pre Processor is used for `%s`, "
                        "advanced has failed with `%s`\n" % (path, e)
                    )
                candidates = LibBuilderBase.CLASSIC_SCANNER(
                    self.env.File(path), self.env, tuple(include_dirs)
                )

            # print(path, [c.get_abspath() for c in candidates])
            for item in candidates:
                if item not in result:
                    result.append(item)
                if not self.PARSE_SRC_BY_H_NAME:
                    continue
                _h_path = item.get_abspath()
                if not fs.path_endswith_ext(_h_path, piotool.SRC_HEADER_EXT):
                    continue
                _f_part = _h_path[: _h_path.rindex(".")]
                for ext in piotool.SRC_C_EXT + piotool.SRC_CXX_EXT:
                    if not os.path.isfile("%s.%s" % (_f_part, ext)):
                        continue
                    _c_path = self.env.File("%s.%s" % (_f_part, ext))
                    if _c_path not in result:
                        result.append(_c_path)

        return result

    def depend_recursive(self, lb, search_files=None):
        def _already_depends(_lb):
            if self in _lb.depbuilders:
                return True
            for __lb in _lb.depbuilders:
                if _already_depends(__lb):
                    return True
            return False

        # assert isinstance(lb, LibBuilderBase)
        if self != lb:
            if _already_depends(lb):
                if self.verbose:
                    sys.stderr.write(
                        "Warning! Circular dependencies detected "
                        "between `%s` and `%s`\n" % (self.path, lb.path)
                    )
                self._circular_deps.append(lb)
            elif lb not in self._depbuilders:
                self._depbuilders.append(lb)
                LibBuilderBase._INCLUDE_DIRS_CACHE = None
        lb.search_deps_recursive(search_files)

    def search_deps_recursive(self, search_files=None):
        if not self._is_dependent:
            self._is_dependent = True
            self.process_dependencies()

            if self.lib_ldf_mode.startswith("deep"):
                search_files = self.get_search_files()

        # when LDF is disabled
        if self.lib_ldf_mode == "off":
            return

        lib_inc_map = {}
        for inc in self._get_found_includes(search_files):
            for lb in self.env.GetLibBuilders():
                if inc.get_abspath() in lb:
                    if lb not in lib_inc_map:
                        lib_inc_map[lb] = []
                    lib_inc_map[lb].append(inc.get_abspath())
                    break

        for lb, lb_search_files in lib_inc_map.items():
            self.depend_recursive(lb, lb_search_files)

    def build(self):  # pylint: disable=too-many-branches
        if self.extra_script and not self._is_built:
            with fs.cd(self.path):
                self.env.SConscriptChdir(1)
                self.env.SConscript(
                    os.path.abspath(self.extra_script),
                    exports={"env": self.env, "pio_lib_builder": self},
                )

        libs = []
        for lb in self._depbuilders:
            libs.extend(lb.build())
            # copy shared information to self env
            for key in ("CPPPATH", "LIBPATH", "LIBS", "LINKFLAGS"):
                self.env.PrependUnique(**{key: lb.env.get(key)})

        for lb in self._circular_deps:
            self.env.PrependUnique(CPPPATH=lb.get_include_dirs())

        if self._is_built:
            return libs

        self._is_built = True
        self.env.PrependUnique(CPPPATH=self.get_include_dirs())

        if self.lib_ldf_mode == "off":
            for lb in self.env.GetLibBuilders():
                if self == lb or not lb.is_built:
                    continue
                for key in ("CPPPATH", "LIBPATH", "LIBS", "LINKFLAGS"):
                    self.env.PrependUnique(**{key: lb.env.get(key)})

        do_not_archive = not self.lib_archive
        if not do_not_archive:
            nodes = self.env.CollectBuildFiles(
                self.build_dir, self.src_dir, self.src_filter
            )
            if nodes:
                libs.append(
                    self.env.BuildLibrary(
                        self.build_dir, self.src_dir, self.src_filter, nodes
                    )
                )
            else:
                do_not_archive = True
        if do_not_archive:
            self.env.BuildSources(self.build_dir, self.src_dir, self.src_filter)

        return libs


class UnknownLibBuilder(LibBuilderBase):
    pass


class ArduinoLibBuilder(LibBuilderBase):
    def load_manifest(self):
        manifest_path = os.path.join(self.path, "library.properties")
        if not os.path.isfile(manifest_path):
            return {}
        return ManifestParserFactory.new_from_file(manifest_path).as_dict()

    @property
    def include_dir(self):
        if not all(
            os.path.isdir(os.path.join(self.path, d)) for d in ("include", "src")
        ):
            return None
        return os.path.join(self.path, "include")

    def get_include_dirs(self):
        include_dirs = LibBuilderBase.get_include_dirs(self)
        if os.path.isdir(os.path.join(self.path, "src")):
            return include_dirs
        if os.path.isdir(os.path.join(self.path, "utility")):
            include_dirs.append(os.path.join(self.path, "utility"))
        return include_dirs

    @property
    def src_filter(self):
        src_dir = os.path.join(self.path, "src")
        if os.path.isdir(src_dir):
            # pylint: disable=no-member
            src_filter = LibBuilderBase.src_filter.fget(self)
            for root, _, files in os.walk(src_dir, followlinks=True):
                found = False
                for fname in files:
                    if fname.lower().endswith("asm"):
                        found = True
                        break
                if not found:
                    continue
                rel_path = root.replace(src_dir, "")
                if rel_path.startswith(os.path.sep):
                    rel_path = rel_path[1:] + os.path.sep
                src_filter.append("-<%s*.[aA][sS][mM]>" % rel_path)
            return src_filter

        src_filter = []
        is_utility = os.path.isdir(os.path.join(self.path, "utility"))
        for ext in piotool.SRC_BUILD_EXT + piotool.SRC_HEADER_EXT:
            # arduino ide ignores files with .asm or .ASM extensions
            if ext.lower() == "asm":
                continue
            src_filter.append("+<*.%s>" % ext)
            if is_utility:
                src_filter.append("+<utility%s*.%s>" % (os.path.sep, ext))
        return src_filter

    @property
    def dependencies(self):
        # do not include automatically all libraries for build
        # chain+ will decide later
        return None

    @property
    def lib_ldf_mode(self):
        # pylint: disable=no-member
        if not self._manifest.get("dependencies"):
            return LibBuilderBase.lib_ldf_mode.fget(self)
        missing = object()
        global_value = self.env.GetProjectConfig().getraw(
            "env:" + self.env["PIOENV"], "lib_ldf_mode", missing
        )
        if global_value != missing:
            return LibBuilderBase.lib_ldf_mode.fget(self)
        # automatically enable C++ Preprocessing in runtime
        # (Arduino IDE has this behavior)
        return "chain+"

    def is_frameworks_compatible(self, frameworks):
        return util.items_in_list(frameworks, ["arduino", "energia"])

    def is_platforms_compatible(self, platforms):
        return util.items_in_list(platforms, self._manifest.get("platforms") or ["*"])

    @property
    def build_flags(self):
        ldflags = [
            LibBuilderBase.build_flags.fget(self),  # pylint: disable=no-member
            self._manifest.get("ldflags"),
        ]
        if self._manifest.get("precompiled") in ("true", "full"):
            # add to LDPATH {build.mcu} folder
            board_config = self.env.BoardConfig()
            for key in ("build.mcu", "build.cpu"):
                libpath = os.path.join(self.src_dir, board_config.get(key, ""))
                if not os.path.isdir(libpath):
                    continue
                self.env.PrependUnique(LIBPATH=libpath)
                break
        ldflags = [flag for flag in ldflags if flag]  # remove empty
        return " ".join(ldflags) if ldflags else None


class MbedLibBuilder(LibBuilderBase):
    def load_manifest(self):
        manifest_path = os.path.join(self.path, "module.json")
        if not os.path.isfile(manifest_path):
            return {}
        return ManifestParserFactory.new_from_file(manifest_path).as_dict()

    @property
    def src_dir(self):
        if os.path.isdir(os.path.join(self.path, "source")):
            return os.path.join(self.path, "source")
        return LibBuilderBase.src_dir.fget(self)  # pylint: disable=no-member

    def get_include_dirs(self):
        include_dirs = LibBuilderBase.get_include_dirs(self)
        if self.path not in include_dirs:
            include_dirs.append(self.path)

        # library with module.json
        for p in self._manifest.get("extraIncludes", []):
            include_dirs.append(os.path.join(self.path, p))

        # old mbed library without manifest, add to CPPPATH all folders
        if not self._manifest:
            for root, _, __ in os.walk(self.path):
                part = root.replace(self.path, "").lower()
                if any(s in part for s in ("%s." % os.path.sep, "test", "example")):
                    continue
                if root not in include_dirs:
                    include_dirs.append(root)

        return include_dirs

    def is_frameworks_compatible(self, frameworks):
        return util.items_in_list(frameworks, ["mbed"])

    def process_extra_options(self):
        self._process_mbed_lib_confs()
        return super(MbedLibBuilder, self).process_extra_options()

    def _process_mbed_lib_confs(self):
        mbed_lib_paths = [
            os.path.join(root, "mbed_lib.json")
            for root, _, files in os.walk(self.path)
            if "mbed_lib.json" in files
        ]
        if not mbed_lib_paths:
            return None

        mbed_config_path = None
        for p in self.env.get("CPPPATH"):
            mbed_config_path = os.path.join(self.env.subst(p), "mbed_config.h")
            if os.path.isfile(mbed_config_path):
                break
            mbed_config_path = None
        if not mbed_config_path:
            return None

        macros = {}
        for mbed_lib_path in mbed_lib_paths:
            macros.update(self._mbed_lib_conf_parse_macros(mbed_lib_path))

        self._mbed_conf_append_macros(mbed_config_path, macros)
        return True

    @staticmethod
    def _mbed_normalize_macro(macro):
        name = macro
        value = None
        if "=" in macro:
            name, value = macro.split("=", 1)
        return dict(name=name, value=value)

    def _mbed_lib_conf_parse_macros(self, mbed_lib_path):
        macros = {}
        cppdefines = str(self.env.Flatten(self.env.subst("$CPPDEFINES")))
        manifest = fs.load_json(mbed_lib_path)

        # default macros
        for macro in manifest.get("macros", []):
            macro = self._mbed_normalize_macro(macro)
            macros[macro["name"]] = macro

        # configuration items
        for key, options in manifest.get("config", {}).items():
            if "value" not in options:
                continue
            macros[key] = dict(
                name=options.get("macro_name"), value=options.get("value")
            )

        # overrode items per target
        for target, options in manifest.get("target_overrides", {}).items():
            if target != "*" and "TARGET_" + target not in cppdefines:
                continue
            for macro in options.get("target.macros_add", []):
                macro = self._mbed_normalize_macro(macro)
                macros[macro["name"]] = macro
            for key, value in options.items():
                if not key.startswith("target.") and key in macros:
                    macros[key]["value"] = value

        # normalize macro names
        for key, macro in macros.items():
            if not macro["name"]:
                macro["name"] = key
                if "." not in macro["name"]:
                    macro["name"] = "%s.%s" % (manifest.get("name"), macro["name"])
                macro["name"] = re.sub(
                    r"[^a-z\d]+", "_", macro["name"], flags=re.I
                ).upper()
                macro["name"] = "MBED_CONF_" + macro["name"]
            if isinstance(macro["value"], bool):
                macro["value"] = 1 if macro["value"] else 0

        return {macro["name"]: macro["value"] for macro in macros.values()}

    def _mbed_conf_append_macros(self, mbed_config_path, macros):
        lines = []
        with open(mbed_config_path, encoding="utf8") as fp:
            for line in fp.readlines():
                line = line.strip()
                if line == "#endif":
                    lines.append("// PlatformIO Library Dependency Finder (LDF)")
                    lines.extend(
                        [
                            "#define %s %s" % (name, value if value is not None else "")
                            for name, value in macros.items()
                        ]
                    )
                    lines.append("")
                if not line.startswith("#define"):
                    lines.append(line)
                    continue
                tokens = line.split()
                if len(tokens) < 2 or tokens[1] not in macros:
                    lines.append(line)
        lines.append("")
        with open(mbed_config_path, mode="w", encoding="utf8") as fp:
            fp.write("\n".join(lines))


class PlatformIOLibBuilder(LibBuilderBase):
    def load_manifest(self):
        manifest_path = os.path.join(self.path, "library.json")
        if not os.path.isfile(manifest_path):
            return {}
        return ManifestParserFactory.new_from_file(manifest_path).as_dict()

    def _has_arduino_manifest(self):
        return os.path.isfile(os.path.join(self.path, "library.properties"))

    @property
    def include_dir(self):
        if "includeDir" in self._manifest.get("build", {}):
            with fs.cd(self.path):
                return os.path.abspath(self._manifest.get("build").get("includeDir"))
        return LibBuilderBase.include_dir.fget(self)  # pylint: disable=no-member

    @property
    def src_dir(self):
        if "srcDir" in self._manifest.get("build", {}):
            with fs.cd(self.path):
                return os.path.abspath(self._manifest.get("build").get("srcDir"))
        return LibBuilderBase.src_dir.fget(self)  # pylint: disable=no-member

    @property
    def src_filter(self):
        # pylint: disable=no-member
        if "srcFilter" in self._manifest.get("build", {}):
            return self._manifest.get("build").get("srcFilter")
        if self.env["SRC_FILTER"]:
            return self.env["SRC_FILTER"]
        if self._has_arduino_manifest():
            return ArduinoLibBuilder.src_filter.fget(self)
        return LibBuilderBase.src_filter.fget(self)

    @property
    def build_flags(self):
        if "flags" in self._manifest.get("build", {}):
            return self._manifest.get("build").get("flags")
        return LibBuilderBase.build_flags.fget(self)  # pylint: disable=no-member

    @property
    def build_unflags(self):
        if "unflags" in self._manifest.get("build", {}):
            return self._manifest.get("build").get("unflags")
        return LibBuilderBase.build_unflags.fget(self)  # pylint: disable=no-member

    @property
    def extra_script(self):
        if "extraScript" in self._manifest.get("build", {}):
            return self._manifest.get("build").get("extraScript")
        return LibBuilderBase.extra_script.fget(self)  # pylint: disable=no-member

    @property
    def lib_archive(self):
        missing = object()
        global_value = self.env.GetProjectConfig().getraw(
            "env:" + self.env["PIOENV"], "lib_archive", missing
        )
        if global_value != missing:
            return self.env.GetProjectConfig().get(
                "env:" + self.env["PIOENV"], "lib_archive"
            )
        # pylint: disable=no-member
        return self._manifest.get("build", {}).get(
            "libArchive", LibBuilderBase.lib_archive.fget(self)
        )

    @property
    def lib_ldf_mode(self):
        # pylint: disable=no-member
        return self.validate_ldf_mode(
            self._manifest.get("build", {}).get(
                "libLDFMode", LibBuilderBase.lib_ldf_mode.fget(self)
            )
        )

    @property
    def lib_compat_mode(self):
        # pylint: disable=no-member
        return self.validate_compat_mode(
            self._manifest.get("build", {}).get(
                "libCompatMode", LibBuilderBase.lib_compat_mode.fget(self)
            )
        )

    def is_platforms_compatible(self, platforms):
        return util.items_in_list(platforms, self._manifest.get("platforms") or ["*"])

    def is_frameworks_compatible(self, frameworks):
        return util.items_in_list(frameworks, self._manifest.get("frameworks") or ["*"])

    def get_include_dirs(self):
        include_dirs = LibBuilderBase.get_include_dirs(self)

        # backwards compatibility with PlatformIO 2.0
        if (
            "build" not in self._manifest
            and self._has_arduino_manifest()
            and not os.path.isdir(os.path.join(self.path, "src"))
            and os.path.isdir(os.path.join(self.path, "utility"))
        ):
            include_dirs.append(os.path.join(self.path, "utility"))

        for path in self.env.get("CPPPATH", []):
            if path not in self.envorigin.get("CPPPATH", []):
                include_dirs.append(self.env.subst(path))

        return include_dirs


class ProjectAsLibBuilder(LibBuilderBase):
    def __init__(self, env, *args, **kwargs):
        # backup original value, will be reset in base.__init__
        project_src_filter = env.get("SRC_FILTER")
        super(ProjectAsLibBuilder, self).__init__(env, *args, **kwargs)
        self.env["SRC_FILTER"] = project_src_filter

    @property
    def include_dir(self):
        include_dir = self.env.subst("$PROJECT_INCLUDE_DIR")
        return include_dir if os.path.isdir(include_dir) else None

    @property
    def src_dir(self):
        return self.env.subst("$PROJECT_SRC_DIR")

    def get_include_dirs(self):
        include_dirs = []
        project_include_dir = self.env.subst("$PROJECT_INCLUDE_DIR")
        if os.path.isdir(project_include_dir):
            include_dirs.append(project_include_dir)
        for include_dir in LibBuilderBase.get_include_dirs(self):
            if include_dir not in include_dirs:
                include_dirs.append(include_dir)
        return include_dirs

    def get_search_files(self):
        # project files
        items = LibBuilderBase.get_search_files(self)
        # test files
        if "__test" in COMMAND_LINE_TARGETS:
            items.extend(
                [
                    os.path.join("$PROJECT_TEST_DIR", item)
                    for item in self.env.MatchSourceFiles(
                        "$PROJECT_TEST_DIR", "$PIOTEST_SRC_FILTER"
                    )
                ]
            )
        return items

    @property
    def lib_ldf_mode(self):
        mode = LibBuilderBase.lib_ldf_mode.fget(self)  # pylint: disable=no-member
        if not mode.startswith("chain"):
            return mode
        # parse all project files
        return "deep+" if "+" in mode else "deep"

    @property
    def src_filter(self):
        # pylint: disable=no-member
        return self.env.get("SRC_FILTER") or LibBuilderBase.src_filter.fget(self)

    @property
    def dependencies(self):
        return self.env.GetProjectOption("lib_deps", [])

    def process_extra_options(self):
        # skip for project, options are already processed
        pass

    def install_dependencies(self):
        def _is_builtin(spec):
            for lb in self.env.GetLibBuilders():
                if lb.name == spec:
                    return True
            return False

        not_found_specs = []
        for spec in self.dependencies:
            # check if built-in library
            if _is_builtin(spec):
                continue

            found = False
            for storage_dir in self.env.GetLibSourceDirs():
                lm = LibraryPackageManager(storage_dir)
                if lm.get_package(spec):
                    found = True
                    break
            if not found:
                not_found_specs.append(spec)

        did_install = False
        lm = LibraryPackageManager(
            self.env.subst(os.path.join("$PROJECT_LIBDEPS_DIR", "$PIOENV"))
        )
        for spec in not_found_specs:
            try:
                lm.install(spec)
                did_install = True
            except (HTTPClientError, UnknownPackageError, InternetIsOffline) as e:
                click.secho("Warning! %s" % e, fg="yellow")

        # reset cache
        if did_install:
            DefaultEnvironment().Replace(__PIO_LIB_BUILDERS=None)

    def process_dependencies(self):  # pylint: disable=too-many-branches
        for spec in self.dependencies:
            found = False
            for storage_dir in self.env.GetLibSourceDirs():
                if found:
                    break
                lm = LibraryPackageManager(storage_dir)
                pkg = lm.get_package(spec)
                if not pkg:
                    continue
                for lb in self.env.GetLibBuilders():
                    if pkg.path != lb.path:
                        continue
                    if lb not in self.depbuilders:
                        self.depend_recursive(lb)
                    found = True
                    break
            if found:
                continue

            # look for built-in libraries by a name
            # which don't have package manifest
            for lb in self.env.GetLibBuilders():
                if lb.name != spec:
                    continue
                if lb not in self.depbuilders:
                    self.depend_recursive(lb)
                found = True
                break

    def build(self):
        self._is_built = True  # do not build Project now
        result = LibBuilderBase.build(self)
        self.env.PrependUnique(CPPPATH=self.get_include_dirs())
        return result


def GetLibSourceDirs(env):
    items = env.GetProjectOption("lib_extra_dirs", [])
    items.extend(env["LIBSOURCE_DIRS"])
    return [
        env.subst(fs.expanduser(item) if item.startswith("~") else item)
        for item in items
    ]


def IsCompatibleLibBuilder(env, lb, verbose=int(ARGUMENTS.get("PIOVERBOSE", 0))):
    compat_mode = lb.lib_compat_mode
    if lb.name in env.GetProjectOption("lib_ignore", []):
        if verbose:
            sys.stderr.write("Ignored library %s\n" % lb.path)
        return None
    if compat_mode == "strict" and not lb.is_platforms_compatible(env["PIOPLATFORM"]):
        if verbose:
            sys.stderr.write("Platform incompatible library %s\n" % lb.path)
        return False
    if compat_mode in ("soft", "strict") and not lb.is_frameworks_compatible(
        env.get("PIOFRAMEWORK", [])
    ):
        if verbose:
            sys.stderr.write("Framework incompatible library %s\n" % lb.path)
        return False
    return True


def GetLibBuilders(env):  # pylint: disable=too-many-branches
    if DefaultEnvironment().get("__PIO_LIB_BUILDERS", None) is not None:
        return sorted(
            DefaultEnvironment()["__PIO_LIB_BUILDERS"],
            key=lambda lb: 0 if lb.dependent else 1,
        )

    DefaultEnvironment().Replace(__PIO_LIB_BUILDERS=[])

    verbose = int(ARGUMENTS.get("PIOVERBOSE", 0))
    found_incompat = False

    for storage_dir in env.GetLibSourceDirs():
        storage_dir = os.path.abspath(storage_dir)
        if not os.path.isdir(storage_dir):
            continue
        for item in sorted(os.listdir(storage_dir)):
            lib_dir = os.path.join(storage_dir, item)
            if item == "__cores__" or not os.path.isdir(lib_dir):
                continue
            try:
                lb = LibBuilderFactory.new(env, lib_dir)
            except exception.InvalidJSONFile:
                if verbose:
                    sys.stderr.write(
                        "Skip library with broken manifest: %s\n" % lib_dir
                    )
                continue
            if env.IsCompatibleLibBuilder(lb):
                DefaultEnvironment().Append(__PIO_LIB_BUILDERS=[lb])
            else:
                found_incompat = True

    for lb in env.get("EXTRA_LIB_BUILDERS", []):
        if env.IsCompatibleLibBuilder(lb):
            DefaultEnvironment().Append(__PIO_LIB_BUILDERS=[lb])
        else:
            found_incompat = True

    if verbose and found_incompat:
        sys.stderr.write(
            'More details about "Library Compatibility Mode": '
            "https://docs.platformio.org/page/librarymanager/ldf.html#"
            "ldf-compat-mode\n"
        )

    return DefaultEnvironment()["__PIO_LIB_BUILDERS"]


def ConfigureProjectLibBuilder(env):
    def _correct_found_libs(lib_builders):
        # build full dependency graph
        found_lbs = [lb for lb in lib_builders if lb.dependent]
        for lb in lib_builders:
            if lb in found_lbs:
                lb.search_deps_recursive(lb.get_search_files())
        for lb in lib_builders:
            for deplb in lb.depbuilders[:]:
                if deplb not in found_lbs:
                    lb.depbuilders.remove(deplb)

    def _print_deps_tree(root, level=0):
        margin = "|   " * (level)
        for lb in root.depbuilders:
            title = "<%s>" % lb.name
            pkg = PackageItem(lb.path)
            if pkg.metadata:
                title += " %s" % pkg.metadata.version
            elif lb.version:
                title += " %s" % lb.version
            click.echo("%s|-- %s" % (margin, title), nl=False)
            if int(ARGUMENTS.get("PIOVERBOSE", 0)):
                if pkg.metadata and pkg.metadata.spec.external:
                    click.echo(" [%s]" % pkg.metadata.spec.url, nl=False)
                click.echo(" (", nl=False)
                click.echo(lb.path, nl=False)
                click.echo(")", nl=False)
            click.echo("")
            if lb.depbuilders:
                _print_deps_tree(lb, level + 1)

    project = ProjectAsLibBuilder(env, "$PROJECT_DIR")
    ldf_mode = LibBuilderBase.lib_ldf_mode.fget(project)  # pylint: disable=no-member

    click.echo("LDF: Library Dependency Finder -> https://bit.ly/configure-pio-ldf")
    click.echo(
        "LDF Modes: Finder ~ %s, Compatibility ~ %s"
        % (ldf_mode, project.lib_compat_mode)
    )

    project.install_dependencies()

    lib_builders = env.GetLibBuilders()
    click.echo("Found %d compatible libraries" % len(lib_builders))

    click.echo("Scanning dependencies...")
    project.search_deps_recursive()

    if ldf_mode.startswith("chain") and project.depbuilders:
        _correct_found_libs(lib_builders)

    if project.depbuilders:
        click.echo("Dependency Graph")
        _print_deps_tree(project)
    else:
        click.echo("No dependencies")

    return project


def exists(_):
    return True


def generate(env):
    env.AddMethod(GetLibSourceDirs)
    env.AddMethod(IsCompatibleLibBuilder)
    env.AddMethod(GetLibBuilders)
    env.AddMethod(ConfigureProjectLibBuilder)
    return env
