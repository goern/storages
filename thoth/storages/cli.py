#!/usr/bin/env python3
# thoth-storages
# Copyright(C) 2019 Fridolin Pokorny
#
# This program is free software: you can redistribute it and / or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.

"""A CLI client to thoth-storages library."""

import json
import os
import logging
import sys

import click
import daiquiri
import yaml

from thoth.storages.graph import GraphCache
from thoth.storages.graph import GraphDatabase
from thoth.storages import GraphCacheStore
from thoth.storages import __version__ as thoth_storages_version

daiquiri.setup(level=logging.INFO)
_LOGGER = logging.getLogger("thoth.storages")


def _print_version(ctx, _, value):
    """Print thoth-storages version and exit."""
    if not value or ctx.resilient_parsing:
        return

    click.echo(thoth_storages_version)
    ctx.exit()


@click.group()
@click.pass_context
@click.option(
    "-v",
    "--verbose",
    is_flag=True,
    envvar="THOTH_STORAGES_VERBOSE",
    help="Be verbose about what's going on.",
)
@click.option(
    "--version",
    is_flag=True,
    is_eager=True,
    callback=_print_version,
    expose_value=False,
    help="Print version and exit.",
)
def cli(ctx=None, verbose: bool = False):
    """CLI tool for interacting with Thoth."""
    if ctx:
        ctx.auto_envvar_prefix = "THOTH_STORAGES"

    if verbose:
        _LOGGER.setLevel(logging.DEBUG)
        _LOGGER.debug("Debug mode is on")


@cli.command("create-cache")
@click.argument(
    "cache_file",
    type=str,
    required=True,
    envvar=GraphCache.ENV_CACHE_PATH,
    default=GraphCache.DEFAULT_CACHE_PATH,
    metavar="CACHE_FILE",
)
@click.option(
    "--cache-config",
    "-c",
    type=str,
    required=True,
    envvar="THOTH_STORAGES_GRAPH_CACHE_CONFIG",
    metavar="CACHE_CONFIG.yaml",
    help="A path to cache configuration file.",
)
def create_graph_cache(cache_file: str, cache_config: str):
    """Interact with thoth-storages' GraphCache to cache results."""
    packages = []
    try:
        with open(cache_config, "r") as dump_file:
            content = yaml.safe_load(dump_file)

        for package_name in content["python-packages"]:
            packages.append(package_name)
    except Exception as exc:
        _LOGGER.exception(
            "Failed to load cache configuration file %r with packages to have in the cache dump: %s",
            cache_config,
            str(exc)
        )
        sys.exit(1)

    graph = GraphDatabase(cache=GraphCache.load(cache_file))
    graph.connect()

    # Fill in the cache:
    for package_name in packages:
        versions = graph.get_all_versions_python_package(
            package_name=package_name,
            only_known_index=True,
            only_solved=True
        )

        if not versions:
            _LOGGER.warning("No records were found for package %r in the graph database", package_name)
            continue

        for package_version, index_url in versions:
            _LOGGER.info("Adding record for %r in version %r from %r", package_name, package_version, index_url)
            graph.retrieve_transitive_dependencies_python(package_name, package_version, index_url)

    report = {
        "cache_file": cache_file,
        "cache_file_size": None,
        "cache_stats": graph.cache.stats(),
    }
    try:
        cache_file_size = os.path.getsize(cache_file)
        report["cache_file_size"] = cache_file_size
    except Exception as exc:
        _LOGGER.error("Failed to obtain cache size: %s", str(exc))

    click.echo(json.dumps(report, indent=2))


@cli.command("store-cache")
@click.argument(
    "cache_file",
    type=str,
    required=True,
    envvar=GraphCache.ENV_CACHE_PATH,
    default=GraphCache.DEFAULT_CACHE_PATH,
    metavar="CACHE_FILE",
)
def store_cache(cache_file: str):
    """Store the given graph cache onto Ceph."""
    graph_cache_store = GraphCacheStore()
    graph_cache_store.connect()
    graph_cache_store.store(cache_file)


@cli.command("restore-cache")
@click.argument(
    "cache_file",
    type=str,
    required=True,
    envvar=GraphCache.ENV_CACHE_PATH,
    default=GraphCache.DEFAULT_CACHE_PATH,
    metavar="CACHE_FILE",
)
def store_cache(cache_file: str):
    """Store the given graph cache onto Ceph."""
    graph_cache_store = GraphCacheStore()
    graph_cache_store.connect()
    graph_cache_store.retrieve(cache_file)


if __name__ == "__main__":
    cli()
