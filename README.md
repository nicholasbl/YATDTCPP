# YATDTCPP
Yet Another Terrible Dependency Tool for CPP

=====

This tool read a dependancy TXT or JSON file, fetches listed packages, builds them, and installs them into a directory called `third_party` in the same directory as the script. Point your build tool at this directory and you are good to go.

The intent of this tool is to be shipped with your repository to deliver needed libraries in a consistent manner, in the absense of any extant package manager, or a package manager that is out of date. This tool can be executed on any platform with python3, and requires no extra packages for python to be installed. There are other source managers out there (like spack) but these are massive, rely on a package directory (which your library might not exist in), etc. This tool just fetches from the url you tell it to, and builds.

Other tools like CPM are great, but are limited to specific build tools (like CMake), and tend to leak configuration from other libraries from poorly written scripts.

Packages are installed in order, so you can easily flatten your dependancy tree.

Take a look at the `install_deps.py` file for more details.
