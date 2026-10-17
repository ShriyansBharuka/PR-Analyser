from linkwatch.checker import check_site, fetch
from linkwatch.config import Site


def site(url, name="test"):
    return Site(name=name, url=url)


def test_ok_is_up(server):
    result = check_site(site(server + "/ok"))
    assert result["up"] is True
    assert result["status"] == 200
    assert result["title"] == "linkwatch test page"


def test_missing_is_down(server):
    result = check_site(site(server + "/missing"))
    assert result["up"] is False
    assert result["status"] == 404


def test_redirect_is_up(server):
    result = check_site(site(server + "/redirect"))
    assert result["up"] is True
    assert result["status"] == 200


def test_slow_is_up(server):
    result = check_site(site(server + "/slow"))
    assert result["up"] is True


def test_closed_port_is_down(closed_port):
    result = check_site(site("http://127.0.0.1:%d/" % closed_port))
    assert result["up"] is False
    assert result["reason"] == "connection error"


def test_latin1_title(server):
    result = check_site(site(server + "/latin1"))
    assert result["title"] == "Café Crème"


def test_user_agent(server):
    resp = fetch(server + "/echo-ua")
    assert resp.text == "linkwatch/1.0"
