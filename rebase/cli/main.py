import sys
import click
import logging

from rebase.version import __version__ as version
from .commands import *

logging.basicConfig(format='%(asctime)s:%(name)s:%(levelname)s:%(message)s', level=logging.INFO, stream=sys.stdout)

def cli_app():
    @click.group()
    @click.version_option(version)
    @click.pass_context
    def cli(ctx):
        pass

    for vname, val in globals().items():
        if isinstance(val, click.Command) or isinstance(val, click.Group):
            cli.add_command(val)

    return cli

def main():    
    app = cli_app()
    return app()

if __name__ == "__main__":
    sys.exit(main())