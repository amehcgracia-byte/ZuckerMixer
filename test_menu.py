import re
from pathlib import Path

import jam_app as app
import mac_app

SPECIAL_ACTIONS = {'guide', 'about', 'github'}


def menu_actions():
    return [item[1] for _title, items in mac_app.MENU_LAYOUT for item in items if item]


def test_every_menu_action_is_a_window_button():
    html = Path('templates/index.html').read_text(encoding='utf-8')
    ids = set(re.findall(r'id="([^"]+)"', html))
    for action in menu_actions():
        assert action in SPECIAL_ACTIONS or action in ids, f'menu action {action!r} has no button in index.html'
    assert {'checkUpdate', 'guide', 'about'} <= set(menu_actions())


def test_menu_items_dispatch_off_the_ui_thread():
    import threading
    calls = []; done = threading.Event()
    def run(name):
        calls.append((name, threading.current_thread() is threading.main_thread())); done.set()
    menus = mac_app.build_menu(run)
    file_menu = next(menu for menu in menus if menu.title == 'File')
    file_menu.items[0].function()
    assert done.wait(2)
    assert calls == [('changeSourceFolder', False)]
    titles = [menu.title for menu in menus]
    assert titles[-4:] == ['File', 'Songs', 'Mix', 'Help']


def test_about_serves_bundled_credits_without_comments():
    about = app.app.test_client().get('/api/about').get_json()
    assert 'José Manuel García' in about['credits_html']
    assert 'GNU General Public License v3.0' in about['credits_html']
    assert '<!--' not in about['credits_html']
    assert about['app_version']
    spec = Path('zucker_mixer.spec').read_text(encoding='utf-8')
    assert '("Credits.html", ".")' in spec
