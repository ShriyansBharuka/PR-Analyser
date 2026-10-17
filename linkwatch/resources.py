import pkg_resources

TEMPLATE_NAME = "status.html.j2"


def template_dir():
    return pkg_resources.resource_filename("linkwatch", "templates")
