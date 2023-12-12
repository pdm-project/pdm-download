# pdm-download

A PDM plugin to download all packages in a lockfile for offline use.


## Installation

```bash
pdm self add pdm-download
```

## Usage

This plugin adds a new command `pdm download`, with the following options:

```bash
pdm download --help
Usage: pdm download [-h] [-L LOCKFILE] [-v | -q] [-g] [-p PROJECT_PATH] [-d DEST]

Download all packages from a lockfile for offline use

Options:
  -h, --help            Show this help message and exit.
  -L LOCKFILE, --lockfile LOCKFILE
                        Specify another lockfile path. Default: pdm.lock. [env var: PDM_LOCKFILE]
  -v, --verbose         Use `-v` for detailed output and `-vv` for more detailed
  -q, --quiet           Suppress output
  -g, --global          Use the global project, supply the project root with `-p` option
  -p PROJECT_PATH, --project PROJECT_PATH
                        Specify another path as the project root, which changes the base of pyproject.toml and __pypackages__ [env var: PDM_PROJECT]
  -d DEST, --dest DEST  The destination directory, default to './packages'
```
