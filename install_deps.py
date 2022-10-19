#!/usr/bin/env python3

'''
This script consumes a list of dependencies (deps.txt or deps.json) that is in
the same directory as this script. The deps will be built and installed in a
`third_party` directory, that has a ./bin, ./lib, ./include, etc. Downloaded
archives are stored in a `third_party_cache` directory for reuse if need be.

== File formats
Deps can be specified in a quick text format or json.

=== Text
An example in text:
```
# This is a comment
- awesome_library
type : cmake
src : https://url/to/some.tar.bz2
options :
    CMAKE_OPTION_ONE ON
    CMAKE_OPTION_TWO OFF

- awesome_library2
...

```
An entry in text format starts with a dash and the name you want to use to refer
to the package. It is followed by attributes which start with a key, followed
by a colon, then followed by content. To make an attribute with list content,
leave a blank on the key line, and follow it with list value lines that start
with a tab or some kind of white space.

Comments start the line with a `#`.

=== JSON
While harder to write, this script also consumes JSON.

An example in json:

```json
[
    {
        "name" : "awesome_library",
        "type" : "cmake",
        "src"  : "https://url/to/some.tar.bz2",
        "options" : [
            "CMAKE_OPTION_ONE ON",
            "CMAKE_OPTION_TWO OFF"
        ]
    },
    ...
```

== Deps and Drivers

To build a package the script needs to know how to tackle the build job; this
is specified in a driver.

=== Common driver attributes
- name (json required, - for text): a name for a package; anything can be put here as long as it is unique.
- type (required): the driver to use for the package
- src (required): url where to download the package
- options (optional): a list of configuration options to pass to the package, interpreted in a driver specific way
- options+flag1+flag2 (optional): options that should only be passed on certain architectures or platforms

=== Flags
System flags are determined by the python platform:
    platform.machine(), platform.system().lower()

=== CMake driver (type: cmake)
For any package that has a CMakeLists.txt. Tries to find a CMakeLists.txt that
has the shortest path in the package, configures and compiles in release mode.
Has no other special flags at this time.

=== Boost driver (type: boost)
Used for a package that uses bjam (like boost). Note that this is pretty rough
and likely only works for boost itself, thus the limited driver name.

=== Make driver (type: config/make)
Use for packages that use a configure script + make. By default the script
assumes that configure is in the root of the archive.
Options are passed to the configure script as is.

This driver will try to make the target 'all', a custom make target can be
defined with the "target" attribute:
`target : build_sw`

=== Header-only library driver (type: header)
This driver does a simple copy and paste of files, thus the `options` attribute
doesnt have much meaning.

This driver, however, does require the `interface` attribute which is an archive
relative path to the directory you wish to copy out. The destination will be
`./third_party/include/package_name`.

== Attribution Support
Packages are scanned for a licence text file; if found, they are collected and
written to `third_party/include/attribution.h` as a constant for inclusion in
your source.

== Build Failures

If the build fails, a log will be created to help you debug the issue. Once the
dep file has been updated, simply re-run the script to build the problematic
package(s).

'''


import argparse
import datetime
import json
import subprocess
import os
import re
import glob
import urllib.request
import urllib.parse
import platform
import shutil
import io
import zlib
import multiprocessing

if __name__ != "__main__":
    print("Not intended to be included as a module")
    exit(1)

parser = argparse.ArgumentParser(description='Build deps')

parser.add_argument(
    '--package',
    help='Build only a specific package'
)

parser.add_argument(
    '--purge',
    action="store_true",
    help='Remove the third party build directory before any other action'
)

parser.add_argument(
    '--force',
    action="store_true",
    help='Reinstall packages regardless of their currently installed status'
)

args = parser.parse_args()

# The root directory where we should build our third party software dir
root = os.path.dirname(os.path.realpath(__file__))


def root_path(p):
    '''Simplify root-relative paths'''
    return os.path.join(root, p)


class DepFileParser:
    def __init__(self):
        self.all = []
        self.current = {}

        self.lines = []
        self.line_no = 1

    def has_line(self):
        return len(self.lines)

    def curr_line(self):
        return self.lines[0]

    def take_line(self):
        ret = self.lines[0]
        self.lines = self.lines[1:]
        self.line_no += 1
        return ret

    def flush(self):
        if len(self.current):
            self.all += [self.current]
        self.current = {}

    def handle_attrib(self):
        line = self.take_line()

        try:
            place = line.index(':')
            parts = [ i.strip() for i in [line[:place], line[place+1:]] ]
            parts = [ i for i in parts if len(i)]

            attrib_name = parts[0]

            content_arr = []

            is_array = True

            if len(parts) > 1:
                is_array = False
                content_arr.append(parts[1])

            #check next line to see if its a continuation
            while self.has_line():
                nextl = self.curr_line()
                if not re.match(r'\s', nextl):
                    break

                if not len(nextl.strip()):
                    break
                content_arr.append(nextl.strip())
                self.take_line()

            # no more content
            if not is_array:
                assert(len(content_arr) == 1)
                content_arr = content_arr[0]

            self.current[attrib_name] = content_arr

            #print("ATTRIB:", attrib_name, self.current[attrib_name])

        except:
            err_at = f"Malformed attribute line: {self.line_no} {line}"
            raise Exception(err_at)

    def handle_line(self):
        if not self.has_line(): return

        line = self.curr_line()

        if not len(line.strip()):
            self.take_line()
            return

        if line.startswith('#'):
            self.take_line()
            return

        if line.startswith('-'):
            self.flush();
            parts = line[1:].split()
            assert(len(parts) >= 1)
            self.current["name"] = parts[0]
            self.take_line()
            #print("header", self.current_name)
            return

        self.handle_attrib()


    def parse(self, p):
        with open(p) as raw_file:
            self.lines = raw_file.readlines()

        while self.has_line():
            self.handle_line()
        self.flush()

        return self.all

# The root third party directory
thirdparty_dir = root_path("third_party")

# The dir to cache downloads
thirdparty_cache_dir = root_path("third_party_cache")

# The path in the third party dir to indicate what is installed
success_file = os.path.join(thirdparty_dir, "installed.txt")

if args.purge:
    shutil.rmtree(thirdparty_dir)

# Create third party dir if it does not exist
if not os.path.exists(thirdparty_dir):
    os.mkdir(thirdparty_dir)

# Create cache dir if it does not exist
if not os.path.exists(thirdparty_cache_dir):
    os.mkdir(thirdparty_cache_dir)

# Try to load the package list
sources = None
try:
    dep_source_parser = DepFileParser()
    sources = dep_source_parser.parse(root_path("deps.txt"))
except Exception:
    print("Unable to read deps.txt, looking for json...")
    try:
        sources = json.load(open(root_path("deps.json")))
    except Exception:
        print("Unable to read json!")
        raise

# regularize how we use package options; they HAVE to be lists.

for value in sources:
    for key in value:
        if not key.startswith('options'):
            continue
        #force this to a list...
        value[key] = list(value[key])

# Filter based on the user's selection, if there is one
if args.package:
    sources = [s for s in sources if s["name"] == args.package]

if not sources:
    print("Empty package list")
    exit(0)


def flags_apply(sys_flags, opt_flags):
    must_have = []
    must_not_have = []

    for f in opt_flags:
        if f.startswith('!'):
            f = f[1:]
            must_not_have.append(f)
        else:
            must_have.append(f)

    must_have = set(must_have)
    must_not_have = set(must_not_have)

    return must_not_have.isdisjoint(sys_flags) and \
        must_have.issubset(sys_flags)

print("This platform has flags:", ",".join(set([platform.machine(), platform.system().lower()])))

def compute_options(node):
    this_system = set([platform.machine(), platform.system().lower()])
    keys = [k for k in node if k.startswith("options")]

    valid_opts = []

    for key in keys:
        parts = key.split('+')[1:]
        these_opts = node[key]
        if flags_apply(this_system, parts):
            valid_opts += these_opts

    return valid_opts


PKGType_CMAKE = "cmake"
PKGType_HEADER_ONLY = "header"
PKGType_CONFIGURE = "config/make"
PKGType_BJAM = "boost"


class Source:
    def __init__(self, source):
        self.name = source["name"].strip()
        self.type = source["type"].strip()
        self.src = source["src"].strip()
        self.package_dir = os.path.join(thirdparty_dir, self.name)
        self.package_unpack_dir = os.path.join(self.package_dir, "src")
        self.package_build_dir = os.path.join(self.package_dir, "build")
        self.package_log_dir = os.path.join(thirdparty_dir, "log", self.name)

        packname = urllib.parse.urlparse(self.src).path.split('/')[-1]

        self.packfile = self.name + packname

        self.package_file = os.path.join(thirdparty_cache_dir, self.packfile)
        self.opts = compute_options(source)

        if self.type == PKGType_HEADER_ONLY:
            self.interface = source["interface"]

        if self.type == PKGType_CONFIGURE and 'target' in source:
            self.target = source['target']

        # for attr in dir(self):
        #     print(f"PKG {attr} = {getattr(self, attr)}")

    def create_log(self):
        print(f"Creating log file for {self.name}")
        self.logfile = get_log_file(self, "log")
        return self

    def finalize(self):
        self.logfile.close()
        pass


def download(s: Source):
    print(f"Downloading {s.src} to {s.package_file}")

    with urllib.request.urlopen(s.src) as response:
        content_len = response.getheader("content-length")
        block_size = -1

        print("Content length", content_len)

        if content_len:
            content_len = int(content_len)
            block_size = max(4096, content_len // 20)

        with open(s.package_file, 'bw') as f:

            if block_size < 0:
                f.write(response.read())
                return

            buffer = io.BytesIO()
            buffer_size = 0
            while True:
                this_buffer = response.read(block_size)
                if not this_buffer:
                    break

                buffer.write(this_buffer)
                buffer_size += len(this_buffer)

                pct = int((buffer_size / content_len)*100)
                print(f"Download: {pct}% of {content_len}")
            buffer.seek(0)
            shutil.copyfileobj(buffer, f)


def write_success(s: Source):
    url_hash = zlib.crc32(str.encode(s.src))
    with open(success_file, 'a') as f:
        f.write(f"{s.name}:{url_hash}\n")


def is_installed(s: Source):
    try:
        srchash = zlib.crc32(str.encode(s.src))
        with open(success_file, 'r') as f:
            lines = f.readlines()
        for l in lines:
            parts = [i.strip() for i in l.split(':')]
            if s.name == parts[0] and srchash == int(parts[1]):
                return True
    except Exception:
        pass
    return False


def find_shortest_path_to(dir, pattern, exclude=None):
    n = dir+"/**/"+pattern

    pl = glob.glob(n, recursive=True)

    n = dir+"/**/"+pattern.upper()

    pl += glob.glob(n, recursive=True)

    n = dir+"/**/"+pattern.lower()

    pl += glob.glob(n, recursive=True)

    if isinstance(exclude, str):
        pl = [p for p in pl if exclude not in p]

    if isinstance(exclude, list):
        for l in exclude:
            pl = [p for p in pl if l not in p]

    pl.sort(key=lambda x: len(x.split(os.pathsep)))
    return pl[0]


def get_log_file(s: Source, prefix):
    now = datetime.datetime.now()
    time_string = now.strftime("%d-%m-%Y_%H-%M-%S")
    fname = f"{prefix}_{time_string}.txt"
    log_path = os.path.join(s.package_log_dir, fname)
    return open(log_path, 'w')


def run_subproc(s: Source, args, cwd=None):
    if not cwd:
        cwd = os.getcwd()
    print("Running", cwd, " ".join(args))
    subprocess.run(args,
                   check=True,
                   cwd=cwd,
                   env=os.environ.copy(),
                   stdout=s.logfile,
                   stderr=s.logfile)

# CMAKE handler ===============================================================


def cmake_strategy(source: Source):
    cmake_exe = "cmake"
    # where is the source cmake?

    try:
        cmakelist = find_shortest_path_to(
            source.package_unpack_dir,
            "CMakeLists.txt"
        )
    except Exception:
        print("Unable to find a valid CMakeLists!")
        raise

    cmakedir = os.path.dirname(cmakelist)

    print("Found CMakeList at", cmakelist)

    def run_cmake_command(opt_list):
        args = [cmake_exe] + opt_list

        print("Running", " ".join(args))

        run_subproc(source, args)

    cmake_options = [
        ("CMAKE_INSTALL_PREFIX", thirdparty_dir),
        ("CMAKE_PREFIX_PATH", thirdparty_dir),
        ("CMAKE_SYSTEM_PREFIX_PATH", thirdparty_dir),
        # ("CMAKE_FIND_USE_SYSTEM_ENVIRONMENT_PATH", "FALSE"),
        # ("CMAKE_FIND_PACKAGE_NO_PACKAGE_REGISTRY", "TRUE"),
        # ("CMAKE_FIND_PACKAGE_NO_SYSTEM_PACKAGE_REGISTRY", "TRUE"),
        ("CMAKE_POSITION_INDEPENDENT_CODE", "ON"),
        ("CMAKE_FIND_ROOT_PATH", thirdparty_dir),
        ("CMAKE_BUILD_TYPE", "Release"),
    ]

    cmake_options += [opt.split() for opt in source.opts]
    cmake_options = ["-D{}={}".format(opt[0], opt[1]) for opt in cmake_options]

    config_command = ["-S", cmakedir] \
        + ["-B", source.package_build_dir, ] \
        + cmake_options

    run_cmake_command(config_command)

    print("Configured. Building...")

    build_command = [
        "--build", source.package_build_dir,
        "-j", str(multiprocessing.cpu_count())
    ]

    print("Running", " ".join(build_command))

    run_cmake_command(build_command)

    print("Built. Installing...")

    install_command = ["--build", source.package_build_dir]
    install_command += ["--target install"]

    run_cmake_command(install_command)


# HeaderOnly handler ==========================================================


def header_only_strategy(s: Source):
    header_dir = s.interface
    header_dir = os.path.join(s.package_unpack_dir, header_dir)

    dest_path = os.path.join(thirdparty_dir, "include", s.name)

    shutil.rmtree(dest_path, ignore_errors=True)

    shutil.copytree(header_dir, dest_path)


# Configure/Make handler ======================================================

def find_configure_script(dir):
    try:
        return find_shortest_path_to(dir, "configure")
    except Exception:
        return find_shortest_path_to(dir, "Configure")


def configmake_driver(s: Source, cscript, cscriptdir):
    opt_list = [
        "--prefix=" + thirdparty_dir
    ]

    args = ['./' + cscript] + opt_list + s.opts

    print("Running", " ".join(args))

    run_subproc(s, args, cscriptdir)

    print("Configured. Building...")

    target = "all"

    try:
        target = s.target
    except Exception:
        pass

    opt_list = [
        "-j" + str(multiprocessing.cpu_count()),
        target
    ]

    run_subproc(s, ["make"] + opt_list, cscriptdir)
    run_subproc(s, ["make", "install"], cscriptdir)


def configmake_strategy(s: Source):
    try:
        cscript_raw = find_configure_script(s.package_unpack_dir)
        cscript = os.path.basename(cscript_raw)
        cscriptdir = os.path.dirname(cscript_raw)
        print("Using config script:", cscript_raw)
    except Exception:
        print("Unable to find a valid configure script!")
        raise

    configmake_driver(s, cscript, cscriptdir)

# BJAM handler ===============================================================


def bjam_bstrap(s: Source, bscript, bscriptdir):
    args = ["sh", "./bootstrap.sh"]

    print("Running", " ".join(args))

    run_subproc(s, args, bscriptdir)


def bjam_build_driver(s: Source, bscriptdir):
    args = ["./b2"]
    args += s.opts
    args += [f"--prefix={thirdparty_dir}", "install"]

    print("Running", " ".join(args))

    run_subproc(s, args, bscriptdir)


def bjam_strategy(s: Source):
    print("BjamStrat")
    # in the future, we can generalize, but right now we only support boost!
    bstrap_script = find_shortest_path_to(s.package_unpack_dir,
                                          "bootstrap.sh")
    bstrap_scriptdir = os.path.dirname(bstrap_script)

    print("BjamStrat", bstrap_script, bstrap_scriptdir)

    bjam_bstrap(s, bstrap_script, bstrap_scriptdir)

    bjam_build_driver(s, bstrap_scriptdir)


# Main Driver =================================================================


build_mapper = {
    PKGType_CMAKE: cmake_strategy,
    PKGType_HEADER_ONLY: header_only_strategy,
    PKGType_CONFIGURE: configmake_strategy,
    PKGType_BJAM: bjam_strategy
}

third_party_info_file_template = '''
#prama once

namespace third_party {
inline constexpr const char* attribution = R"(

<text>

)";

}
'''


def find_write_attribution(pkg):
    try:
        licfile = find_shortest_path_to(pkg.package_unpack_dir,
                                        "licen?e*",
                                        [".hpp", ".h", ".cpp"]
                                        )

        print("Found license file at", licfile)

        with open(licfile) as licfsrc:
            licfile = str(licfsrc.read())

        ret = f"This software may include the package {pkg.name}.\n"
        ret += "This package has the following license:\n"
        ret += licfile

        attrib_path = os.path.join(thirdparty_dir, f"attrib.{pkg.name}.txt")

        print("Writing attribution to", attrib_path)

        with open(attrib_path, 'w') as outfile:
            outfile.write(ret)

    except Exception as e:
        print(e)
        print(f"Warning: no license file for {pkg.name}!")


failed = []

for source in sources:
    pkg = Source(source)
    if is_installed(pkg) and not args.force:
        print(f"Package {pkg.name} is already installed, skipping...")
        continue

    print(f"Building {pkg.name}")

    os.makedirs(pkg.package_dir, exist_ok=True)
    os.makedirs(pkg.package_unpack_dir, exist_ok=True)
    os.makedirs(pkg.package_build_dir, exist_ok=True)
    os.makedirs(pkg.package_log_dir, exist_ok=True)

    pkg.create_log()

    # check if we have it in cache

    need_dl = not os.path.exists(pkg.package_file) or \
        os.path.getsize(pkg.package_file) == 0

    if need_dl:
        download(pkg)

    assert(os.path.exists(pkg.package_file))
    assert(os.path.getsize(pkg.package_file) != 0)

    print("Unpacking...")

    shutil.unpack_archive(pkg.package_file, pkg.package_unpack_dir)

    print("Unpacked. Configuring...")

    try:
        print(f"Using {pkg.type} strategy")
        build_mapper[pkg.type](pkg)
    except Exception as e:
        print("Unable to unstall", pkg.name)
        print("Reason", e)
        shutil.rmtree(pkg.package_dir)
        failed.append(pkg.name)
        raise

    print("Installed. Running post-install tasks...")

    find_write_attribution(pkg)

    print("Cleaning...")

    shutil.rmtree(pkg.package_dir)
    write_success(pkg)
    pkg.finalize()

if failed:
    print("Following packages failed to install:")
    for f in failed:
        print(f"- {f}")


# find all attrib files and glue them into a header
def rebuild_attrib_header():
    attrib_header = os.path.join(thirdparty_dir, "include", "attribution.h")
    paths = glob.glob(thirdparty_dir + "/attrib.*.txt")

    contents = []

    for p in paths:
        with open(p) as f:
            contents.append(f.read())

    contents = "\n----\n".join(contents)

    to_write = third_party_info_file_template.replace("<text>", contents)

    with open(attrib_header, 'w') as f:
        f.write(to_write)


rebuild_attrib_header()
