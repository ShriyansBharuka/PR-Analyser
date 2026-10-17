from datetime import datetime

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .resources import TEMPLATE_NAME, template_dir


def make_env():
    return Environment(
        loader=FileSystemLoader(template_dir()),
        autoescape=select_autoescape(["html", "j2"]),
    )


def render_report(results, output="status.html", title="linkwatch"):
    template = make_env().get_template(TEMPLATE_NAME)
    up = sum(1 for r in results if r.get("up"))
    page = template.render(
        title=title,
        results=results,
        up=up,
        down=len(results) - up,
        generated=datetime.now().strftime("%Y-%m-%d %H:%M"),
    )
    with open(output, "w", encoding="utf-8") as fh:
        fh.write(page)
    return output
