#!/usr/bin/env bash

privy_version() {
  python3 -c \
    "import re; print(re.search(r'__version__\\s*=\\s*\"([^\"]+)\"', open('src/privy/__init__.py').read())[1])"
}
