#!/usr/bin/env python3

import argparse
import datetime
import json
import subprocess
import os
import glob
import urllib.request
import urllib.parse
import platform
import shutil
import io
import zlib
import multiprocessing
from multiprocessing import Process

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
try:
    sources = json.load(open(root_path("deps.json")))
except Exception:
    print("Unable to read json!")
    raise

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

    # print(sys_flags, must_have, must_not_have)

    return must_not_have.isdisjoint(sys_flags) and \
        must_have.issubset(must_have)


def compute_options(node):
    this_system = set([platform.machine, platform.system().lower()])
    keys = [k for k in node if k.startswith("options")]

    valid_opts = []

    for key in keys:
        parts = key.split(':')[1:]
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


def find_shortest_path_to(dir, pattern):
    n = dir+"/**/"+pattern.upper()

    pl = glob.glob(n, recursive=True)

    n = dir+"/**/"+pattern.lower()

    pl += glob.glob(n, recursive=True)

    pl.sort(key=lambda x: x.split(os.pathsep))
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
    subprocess.run(args,
                   check=True,
                   cwd=cwd,
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
        ("CMAKE_FIND_USE_SYSTEM_ENVIRONMENT_PATH", "FALSE"),
        ("CMAKE_FIND_PACKAGE_NO_PACKAGE_REGISTRY", "TRUE"),
        ("CMAKE_FIND_PACKAGE_NO_SYSTEM_PACKAGE_REGISTRY", "TRUE"),
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
    return find_shortest_path_to(dir, "configure")


def configmake_driver(s: Source, cscript, cscriptdir):
    opt_list = [
        "--prefix=" + thirdparty_dir
    ]

    print("Running", " ".join(cscript + opt_list))

    run_subproc(source, cscript + opt_list, cscriptdir)

    print("Configured. Building...")

    opt_list = [
        "-j" + str(multiprocessing.cpu_count())
    ]

    run_subproc(source, ["make"] + opt_list, cscriptdir)
    run_subproc(source, ["make", "install"], cscriptdir)


def configmake_strategy(s: Source):
    try:
        cscript = find_configure_script(s.package_unpack_dir)
        cscriptdir = os.path.dirname(cscript)
    except Exception:
        print("Unable to find a valid configure script!")
        raise

    p = Process(target=configmake_driver, args=(s, cscript, cscriptdir, ))
    p.start()
    p.join()

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
        licfile = find_shortest_path_to(pkg.package_unpack_dir, "licen?e*")

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
