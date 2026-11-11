"""The Tk/ttk form of the Space Arena client (TASK6): profile -> faction -> demo ship -> solo check.

This module imports tkinter; __main__ loads it lazily and turns missing Tk or a
missing display into a clean exit 2. Everything behind the widgets is the core:
Profile/load_profile/save_profile (whitelist, explicit paths only), DEMO_CATALOG
and MockAdapter (demo selection: never a spawn, a slot or a connection),
RelayStub (always error/unavailable), SoloSession (one stock solo c172p, argv
without shell, stdout/stderr inherited). The form itself never connects, never
resolves names, never execs a profile and never terminates any process.
"""
# SPDX-License-Identifier: GPL-2.0-or-later
import json
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .core import (MockAdapter, Profile, RelayStub, SoloSession,
                   load_profile, save_profile, ships)

TclError = tk.TclError  # re-exported so __main__ can catch the no-display case

BG = '#15161a'
PANEL = '#20222a'
FIELD = '#262933'
FG = '#e8e9ee'
MUTED = '#9aa1ad'
OK = '#67c98a'
WARN = '#d9a441'
ERR = '#e2686f'
ACCENT = '#33507e'
JSON_TYPES = [('JSON', '*.json'), ('Все файлы', '*')]


def target_version():
    """Read-only FlightGear target from docs/SOURCES.json; '' when it cannot be read,
    including malformed data such as a list where a mapping is expected."""
    try:
        sources = Path(__file__).resolve().parents[2] / 'docs' / 'SOURCES.json'
        return str(json.loads(sources.read_text(encoding='utf-8'))['flightgear_target']['version'])
    except (OSError, ValueError, KeyError, TypeError):
        return ''


class ClientApp:
    """Main window. Buttons are the only actions, and each label says what it is NOT."""

    def __init__(self, root, profile=None):
        self.root = root
        self.session = SoloSession()
        self.adapter = MockAdapter()
        self._poll_job = None
        root.title('Space Arena — клиент (DEMO)')
        root.configure(bg=BG)
        screen_w = root.winfo_screenwidth()
        screen_h = root.winfo_screenheight()
        self.width = min(960, screen_w)
        self.height = min(720, screen_h)
        root.geometry(f'{self.width}x{self.height}')
        root.minsize(min(560, self.width), min(420, self.height))
        self._style()
        self._vars()
        self._build()
        self._apply_profile(profile or Profile())
        root.protocol('WM_DELETE_WINDOW', self._on_close)

    def _style(self):
        style = ttk.Style(self.root)
        style.theme_use('clam')
        style.configure('.', background=BG, foreground=FG, fieldbackground=FIELD,
                        borderwidth=0)
        style.configure('TFrame', background=BG)
        style.configure('TLabelframe', background=PANEL, foreground=MUTED, padding=8)
        style.configure('TLabelframe.Label', background=PANEL, foreground=MUTED)
        style.configure('TLabel', background=BG, foreground=FG)
        style.configure('Card.TLabel', background=PANEL, foreground=FG)
        style.configure('Muted.TLabel', background=BG, foreground=MUTED)
        style.configure('Muted.Card.TLabel', background=PANEL, foreground=MUTED)
        style.configure('Title.TLabel', background=BG, foreground=FG,
                        font=('TkDefaultFont', 15, 'bold'))
        style.configure('TButton', background='#2d323e', foreground=FG, padding=6)
        style.map('TButton', background=[('active', '#3a4150'), ('disabled', PANEL)],
                  foreground=[('disabled', MUTED)])
        style.configure('Launch.TButton', background='#274427', foreground='#eafbe9',
                        padding=8, font=('TkDefaultFont', 11, 'bold'))
        style.map('Launch.TButton', background=[('active', '#316031'), ('disabled', PANEL)],
                  foreground=[('disabled', MUTED)])
        style.configure('TEntry', insertcolor=FG, padding=5)
        style.configure('TRadiobutton', background=PANEL, foreground=FG, padding=4)
        style.map('TRadiobutton', background=[('active', PANEL)],
                  indicatorcolor=[('selected', '#4f8cff')])
        style.configure('Treeview', background=FIELD, fieldbackground=FIELD,
                        foreground=FG, rowheight=26)
        style.configure('Treeview.Heading', background=PANEL, foreground=MUTED)
        style.map('Treeview', background=[('selected', ACCENT)],
                  foreground=[('selected', FG)])

    def _vars(self):
        self.var_fgfs = tk.StringVar()
        self.var_callsign = tk.StringVar()
        self.var_endpoint = tk.StringVar()
        self.var_team = tk.StringVar(value='rebels')

    def _status_label(self, parent, grid_row=None):
        """A status line; grid inside grid-managed parents, pack inside pack-managed
        ones — Tk forbids mixing the geometry managers under one master."""
        label = ttk.Label(parent, text='', style='Muted.Card.TLabel', wraplength=520,
                          justify='left')
        if grid_row is None:
            label.pack(fill='x', padx=8, pady=(2, 2))
        else:
            label.grid(row=grid_row, column=0, columnspan=3, sticky='w', padx=4, pady=(2, 2))
        return label

    def _say(self, label, text, color=MUTED):
        label.configure(text=text, foreground=color)

    def _build(self):
        outer = ttk.Frame(self.root)
        outer.pack(fill='both', expand=True, padx=14, pady=12)
        head = ttk.Frame(outer)
        head.pack(fill='x')
        ttk.Label(head, text='Space Arena', style='Title.TLabel').pack(anchor='w')
        version = target_version()
        intro = 'DEMO-выбор корабля не означает запуск и не занимает слот.'
        if version:
            intro += '  Target FG: ' + version + ' (read-only, docs/SOURCES.json)'
        ttk.Label(head, text=intro, style='Muted.TLabel', wraplength=540,
                  justify='left').pack(anchor='w', pady=(0, 6))

        scroller = ttk.Frame(outer)
        scroller.pack(fill='both', expand=True)
        self.canvas = tk.Canvas(scroller, bg=BG, highlightthickness=0)
        vsb = ttk.Scrollbar(scroller, orient='vertical', command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side='right', fill='y')
        self.canvas.pack(side='left', fill='both', expand=True)
        body = ttk.Frame(self.canvas)
        self._body_id = self.canvas.create_window((0, 0), window=body, anchor='nw')
        body.bind('<Configure>', self._body_configured)
        self.canvas.bind('<Configure>', self._canvas_configured)
        for sequence in ('<MouseWheel>', '<Button-4>', '<Button-5>'):
            self.canvas.bind_all(sequence, self._on_mousewheel)

        prof = ttk.LabelFrame(body, text='Профиль')
        prof.pack(fill='x', pady=6)
        prof.columnconfigure(1, weight=1)
        ttk.Label(prof, text='fgfs', style='Card.TLabel').grid(row=0, column=0, sticky='w',
                                                               padx=(4, 6), pady=3)
        ttk.Entry(prof, textvariable=self.var_fgfs).grid(row=0, column=1, sticky='we', pady=3)
        ttk.Button(prof, text='…', width=3, command=self._pick_fgfs).grid(
            row=0, column=2, padx=(6, 4))
        ttk.Label(prof, text='callsign', style='Card.TLabel').grid(row=1, column=0, sticky='w',
                                                                   padx=(4, 6), pady=3)
        ttk.Entry(prof, textvariable=self.var_callsign, width=10).grid(row=1, column=1,
                                                                       sticky='w', pady=3)
        ttk.Label(prof, text='endpoint', style='Card.TLabel').grid(row=2, column=0, sticky='w',
                                                                   padx=(4, 6), pady=3)
        ttk.Entry(prof, textvariable=self.var_endpoint).grid(row=2, column=1, columnspan=2,
                                                             sticky='we', padx=(0, 4), pady=3)
        ttk.Label(prof, style='Muted.Card.TLabel', wraplength=520, justify='left',
                  text='endpoint пусто = solo; иначе только wss://host[:port]/mp. '
                       'Профиль читается и пишется только по явному пути.').grid(
            row=3, column=0, columnspan=3, sticky='w', padx=4)
        buttons = ttk.Frame(prof)
        buttons.grid(row=4, column=0, columnspan=3, sticky='w', padx=2, pady=(4, 2))
        ttk.Button(buttons, text='Загрузить…', command=self._load_profile).pack(side='left',
                                                                                padx=4)
        ttk.Button(buttons, text='Сохранить как…', command=self._save_profile).pack(side='left',
                                                                                    padx=4)
        ttk.Label(prof, style='Muted.Card.TLabel', wraplength=520, justify='left',
                  text='Личный профиль держите вне рабочих копий Git '
                       '(имя по умолчанию: space-ui.local.json).').grid(
            row=5, column=0, columnspan=3, sticky='w', padx=4)
        self.profile_status = self._status_label(prof, grid_row=6)

        team = ttk.LabelFrame(body, text='Команда (DEMO-выбор на этой форме)')
        team.pack(fill='x', pady=6)
        for label, value in (('Rebels', 'rebels'), ('Empire', 'empire')):
            ttk.Radiobutton(team, text=label, value=value, variable=self.var_team,
                            command=self._team_changed).pack(side='left', padx=8, pady=2)

        catalog = ttk.LabelFrame(body, text='Каталог (DEMO): выбор — не запуск и не слот')
        catalog.pack(fill='x', pady=6)
        self.tree = ttk.Treeview(catalog, columns=('status', 'reason'), height=4,
                                 selectmode='browse')
        self.tree.heading('#0', text='Корабль')
        self.tree.heading('status', text='Статус')
        self.tree.heading('reason', text='Причина')
        self.tree.column('#0', width=120, stretch=False)
        self.tree.column('status', width=190, stretch=False)
        self.tree.column('reason', width=220)
        self.tree.bind('<<TreeviewSelect>>', self._selection_changed)
        self.tree.pack(fill='x', padx=8)
        ttk.Button(catalog, text='Проверить выбор (DEMO)',
                   command=self._check_choice).pack(anchor='w', padx=8, pady=(6, 2))
        self.demo_status = self._status_label(catalog)

        network = ttk.LabelFrame(body, text='Сеть (relay): интерфейс будущего адаптера')
        network.pack(fill='x', pady=6)
        ttk.Button(network, text='Проверить состояние сети (stub)',
                   command=self._check_network).pack(anchor='w', padx=8)
        self.net_status = self._status_label(network)
        self._say(self.net_status, 'Сеть недоступна / не реализована: relay — только '
                                   'интерфейс; ни соединения, ни обещаний TLS.', MUTED)

        launch = ttk.LabelFrame(body, text='Диагностика запуска: штатный c172p SOLO '
                                           '(не выбранный DEMO-корабль)')
        launch.pack(fill='x', pady=6)
        self.launch_button = ttk.Button(launch, text='Вылет: c172p SOLO',
                                        style='Launch.TButton', command=self._launch)
        self.launch_button.pack(anchor='w', padx=8, pady=(2, 4))
        ttk.Label(launch, style='Muted.Card.TLabel', wraplength=520, justify='left',
                  text='c172p — проверка запускаемого FlightGear, а не X-wing; выбранный '
                       'DEMO-корабль не запускается. stdout/stderr FG наследуются: лог '
                       'в терминале.').pack(anchor='w', padx=8)
        self.launch_status = self._status_label(launch)

    def _body_configured(self, event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox('all') or (0, 0, 0, 0))

    def _canvas_configured(self, event=None):
        self.canvas.itemconfigure(self._body_id, width=event.width)

    def _on_mousewheel(self, event):
        button = getattr(event, 'num', None)
        delta = getattr(event, 'delta', 0)
        if button == 4 or delta > 0:
            self.canvas.yview_scroll(-1, 'units')
        elif button == 5 or delta < 0:
            self.canvas.yview_scroll(1, 'units')

    def _apply_profile(self, profile):
        self.var_fgfs.set(profile.fgfs)
        self.var_callsign.set(profile.callsign)
        self.var_endpoint.set(profile.endpoint)
        self.var_team.set(profile.team_id)
        self._refresh_catalog(select=profile.ship_id)

    def _team_changed(self):
        self._refresh_catalog()  # a new team starts on its first fighter
        self._selection_changed()  # any old DEMO verdict is void after a team switch

    def _refresh_catalog(self, select=None):
        team = self.var_team.get()
        self.tree.delete(*self.tree.get_children())
        ids, first_available = [], None
        for ship in ships(team):
            ids.append(ship.id)
            if ship.available and first_available is None:
                first_available = ship.id
            self.tree.insert('', 'end', iid=ship.id, text=ship.label,
                             values=('DEMO: доступен для проверки' if ship.available
                                     else 'заблокирован', ship.reason),
                             tags=('ok',) if ship.available else ('blocked',))
        self.tree.tag_configure('ok', foreground=OK)
        self.tree.tag_configure('blocked', foreground=MUTED)
        wanted = select if select in ids else first_available
        if wanted:
            self.tree.selection_set(wanted)
            self.tree.see(wanted)

    def _selected_ship(self):
        selection = self.tree.selection()
        if not selection:
            return None
        return next((s for s in ships(self.var_team.get()) if s.id == selection[0]), None)

    def _build_profile(self):
        ship = self._selected_ship()
        return Profile(fgfs=self.var_fgfs.get(), callsign=self.var_callsign.get(),
                       endpoint=self.var_endpoint.get(), team_id=self.var_team.get(),
                       ship_id=ship.id if ship else ships(self.var_team.get())[0].id)

    def _selection_changed(self, event=None):
        """Any team or selection change voids the previous DEMO verdict; a blocked
        ship shows its reason instead of a stale grant."""
        ship = self._selected_ship()
        if ship is None:
            self._say(self.demo_status, 'Выберите корабль в каталоге.', MUTED)
        elif not ship.available:
            self._say(self.demo_status, 'DEMO-выбор недоступен: ' + ship.reason, WARN)
        else:
            self._say(self.demo_status, 'DEMO: ' + ship.label + ' — можно проверить '
                      'выбор; это не запуск и не слот.', MUTED)

    def _load_profile(self):
        path = filedialog.askopenfilename(title='Загрузить профиль', parent=self.root,
                                          filetypes=JSON_TYPES)
        if not path:
            return
        try:
            profile = load_profile(path)
        except ValueError as exc:  # core messages are generic and never echo values
            self._say(self.profile_status, 'Профиль не загружен: ' + str(exc), ERR)
            return
        self._apply_profile(profile)
        self._say(self.profile_status, 'Профиль загружен; выполнено только чтение '
                                       'списка полей — ничего не запущено.', OK)

    def _save_profile(self):
        try:
            profile = self._build_profile()
        except ValueError as exc:
            self._say(self.profile_status, 'Профиль не сохранён: ' + str(exc), ERR)
            return
        path = filedialog.asksaveasfilename(title='Сохранить профиль как', parent=self.root,
                                            defaultextension='.json',
                                            initialfile='space-ui.local.json',
                                            filetypes=JSON_TYPES)
        if not path:
            return
        try:
            save_profile(path, profile)
        except OSError:
            self._say(self.profile_status, 'Профиль не сохранён: файл не записан '
                                           '(проверьте путь и права).', ERR)
            return
        self._say(self.profile_status, 'Сохранён только профиль ' + Path(path).name +
                  '. Не кладите личный профиль в Git.', OK)

    def _pick_fgfs(self):
        path = filedialog.askopenfilename(title='Выберите исполняемый файл fgfs',
                                          parent=self.root,
                                          filetypes=[('FlightGear', 'fgfs fgfs.exe'),
                                                     ('Все файлы', '*')])
        if path:
            self.var_fgfs.set(path)

    def _check_choice(self):
        ship = self._selected_ship()
        if ship is None:
            self._say(self.demo_status, 'Выберите корабль в каталоге.', MUTED)
            return
        granted, reason = self.adapter.authorize(self.var_team.get(), ship.id)
        if granted:
            self._say(self.demo_status, 'DEMO-проверка: выбор разрешён — ' + reason, OK)
        else:
            self._say(self.demo_status, 'DEMO-проверка: отказ — ' + reason, ERR)

    def _check_network(self):
        relay = RelayStub()
        state = relay.connect()
        self._say(self.net_status, 'Проверка stub: состояние «' + state + '» — ' +
                  relay.reason + '. Соединение не устанавливалось.', ERR)

    def _launch(self):
        try:
            profile = self._build_profile()
        except ValueError as exc:
            self._say(self.launch_status, 'Профиль некорректен: ' + str(exc), ERR)
            return
        try:
            child = self.session.launch(profile)
        except (OSError, RuntimeError) as exc:  # core errors are generic
            self._say(self.launch_status, 'Запуск не выполнен: ' + str(exc), ERR)
            return
        self.launch_button.configure(state='disabled')
        pid = getattr(child, 'pid', None)
        self._say(self.launch_status, 'FlightGear (c172p solo) запущен' +
                  (' (PID ' + str(pid) + ')' if pid else '') +
                  '. Запуск процесса ≠ готовность к полёту; лог — в терминале.', WARN)
        self._schedule_poll()

    def _schedule_poll(self):
        if self._poll_job is None:  # exactly one pending timer, whatever happens
            self._poll_job = self.root.after(400, self._poll_child)

    def _poll_child(self):
        self._poll_job = None
        try:
            code = self.session.poll()
        except OSError as exc:
            self.launch_button.configure(state='normal')
            self._say(self.launch_status, 'Опрос процесса не удался (' +
                      type(exc).__name__ + '); клиент ничего не останавливает.', ERR)
            return
        if code is None:
            self._schedule_poll()
            return
        self.launch_button.configure(state='normal')
        if code == 0:
            self._say(self.launch_status, 'Процесс c172p завершился с кодом 0; '
                                          'подробности — в логе терминала.', OK)
        else:
            self._say(self.launch_status, 'Ранний выход: код ' + str(code) + '. '
                      'Проверьте путь fgfs и лог в терминале.', ERR)

    def _on_close(self):
        if self.session.process is not None and self.session.process.poll() is None:
            if not messagebox.askyesno('Закрыть только клиент?',
                                       'FlightGear всё ещё работает.\nЗакрыть только '
                                       'клиент? FG продолжит работу: клиент не '
                                       'останавливает ни его, ни чужие процессы.',
                                       parent=self.root):
                return
        if self._poll_job is not None:
            self.root.after_cancel(self._poll_job)
            self._poll_job = None
        for sequence in ('<MouseWheel>', '<Button-4>', '<Button-5>'):
            self.canvas.unbind_all(sequence)
        self.root.destroy()


def run(profile=None):
    """Open the form and run the Tk mainloop; returns the process exit code.

    A tk.TclError from window construction (no display) propagates to __main__,
    which reports it honestly as exit 2: this module never fakes a windowless GUI."""
    root = tk.Tk()
    ClientApp(root, profile=profile)
    root.mainloop()
    return 0
