import click
from rich.console import Console
from rich.live import Live
from rich.table import Table

from . import __version__, cache, checker, config, resources
from . import http_patch  # noqa: F401

console = Console()


def build_table(title, results):
    table = Table(title=title)
    table.add_column("Site", style="bold")
    table.add_column("Status")
    table.add_column("Code", justify="right")
    table.add_column("Time", justify="right")
    table.add_column("Title", overflow="fold")
    for r in results:
        status = "[green]UP[/green]" if r["up"] else "[red]DOWN[/red]"
        code = str(r["status"]) if r["status"] else r["reason"]
        elapsed = "%d ms" % r["elapsed_ms"] if r["elapsed_ms"] is not None else "-"
        table.add_row(r["name"], status, code, elapsed, r["title"])
    return table


@click.group()
@click.version_option(__version__, prog_name="linkwatch")
def cli():
    """Check your sites and build a small status page."""


@cli.command()
@click.option("--config", "config_path", default="sites.toml", show_default=True,
              help="Path to the sites file.")
def check(config_path):
    """Check all sites and save the results."""
    settings = config.load_settings(config_path)
    results = []
    with Live(build_table(settings.title, results), console=console,
              refresh_per_second=4) as live:
        for result in checker.check_all(settings.sites):
            results.append(result)
            live.update(build_table(settings.title, results))
    path = cache.save_results(results)
    up = sum(1 for r in results if r["up"])
    console.print("%d/%d sites up, results saved to %s" % (up, len(results), path))


@cli.command()
@click.option("--output", "-o", default="status.html", show_default=True,
              help="Where to write the HTML page.")
@click.option("--title", default="linkwatch", show_default=True)
def report(output, title):
    """Render the cached results to an HTML page."""
    from .report import render_report

    results = cache.load_results()
    if not results:
        raise click.ClickException("No cached results, run 'linkwatch check' first.")
    path = render_report(results, output=output, title=title)
    click.echo("Wrote %s" % path)


@cli.command()
def info():
    """Show where linkwatch keeps its files."""
    click.echo("Templates: %s" % resources.template_dir())
    click.echo("Cache:     %s" % cache.cache_file())


if __name__ == "__main__":
    cli()
