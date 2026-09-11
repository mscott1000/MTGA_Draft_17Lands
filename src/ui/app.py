"""
src/ui/app.py
Main UI Orchestrator / Context Root.
Serves as the central registry for the application state, delegating UI construction
to the AppLayoutManager and application lifecycle to the AppController.
"""

import logging
import tkinter
import os
from typing import Dict, Optional

from src import constants
from src.configuration import write_configuration
from src.ui.styles import Theme

from src.ui.orchestrator import DraftOrchestrator
from src.notifications import Notifications

# UI Components
from src.ui.loading_overlay import LoadingOverlay
from src.ui.menu_bar import AppMenuBar
from src.ui.top_bar import TopBarControls
from src.ui.card_interactions import CardInteractionManager
from src.ui.windows.overlay import CompactOverlay
from src.ui.windows.settings import SettingsWindow
from src.auto_launch import ArenaPresenceMonitor, set_auto_launch_enabled

# Delegated Managers
from src.ui.app_layout import AppLayoutManager
from src.ui.app_controller import AppController

logger = logging.getLogger(__name__)


class DraftApp:
    """
    The Core Application Context.
    Connects the logic controllers to the UI views via a clean facade pattern.
    """

    def __init__(
        self,
        root: tkinter.Tk,
        scanner,
        configuration,
        started_in_background: bool = False,
    ):
        self.root = root
        self.configuration = configuration
        self.started_in_background = started_in_background

        # 1. IMMEDIATE STATE INITIALIZATION
        self.vars: Dict[str, tkinter.Variable] = {}
        self.deck_filter_map: Dict[str, str] = {}
        self.overlay_window: Optional[CompactOverlay] = None

        self._initialized = False
        self._rebuilding_ui = False
        self._loading = False

        self.current_pack_data = []
        self.current_missing_data = []

        # Event Tracking State
        self.current_set_data_map = {}
        self.detected_set_code = ""
        self.active_event_set = ""
        self.active_event_type = ""
        self.current_draft_id = ""
        self._presence_monitor = None
        self._arena_seen = False
        self._user_hidden = False

        # 2. INITIAL THEME APPLICATION
        current_scale = constants.UI_SIZE_DICT.get(
            self.configuration.settings.ui_size, 1.0
        )
        Theme.apply(
            self.root,
            palette=self.configuration.settings.theme,
            engine=getattr(self.configuration.settings, "theme_base", "clam"),
            custom_path=getattr(self.configuration.settings, "theme_custom_path", ""),
            scale=current_scale,
        )

        # 3. SET UP DELEGATES & SUB-SYSTEMS
        self._setup_variables()
        self.orchestrator = DraftOrchestrator(
            scanner, configuration, self._refresh_ui_data
        )

        self.layout_manager = AppLayoutManager(self)
        self.controller = AppController(self)
        self.interactions = CardInteractionManager(self)

        # 4. BUILD UI SHELL
        self.layout_manager.build()
        self.menu_bar = AppMenuBar(self.root, self)
        self.loading_overlay = LoadingOverlay(self.root)

        # 5. ATTACH INFRASTRUCTURE SERVICES
        self.notifications = Notifications(
            self.root, scanner.set_list, configuration, self.panel_data
        )

        # 6. VIRTUAL EVENT BINDINGS
        self.root.bind(
            "<<ShowDataTab>>",
            lambda e: (
                self._ensure_tabs_visible() or self.notebook.select(self.panel_data)
            ),
        )

        # 7. FINAL WINDOW PROTOCOL & METADATA
        self.root.title(f"MTGA Draft Tool v{constants.APPLICATION_VERSION}")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.attributes("-topmost", self.configuration.settings.always_on_top)

        self.orchestrator.scanner.log_enable(
            self.configuration.settings.draft_log_enabled
        )

        self._loading = True
        self._initialized = True

    # =========================================================================
    # FACADE UI PROPERTIES (Preserves backward compatibility with other files)
    # =========================================================================

    # Threading access for tests & orchestrator
    @property
    def _update_task_id(self):
        return self.controller._update_task_id

    @_update_task_id.setter
    def _update_task_id(self, value):
        self.controller._update_task_id = value

    @property
    def top_bar(self) -> TopBarControls:
        return self.layout_manager.top_bar

    @property
    def dashboard(self):
        return self.layout_manager.dashboard

    @property
    def notebook(self):
        return self.layout_manager.notebook

    @property
    def panel_taken(self):
        return self.layout_manager.panel_taken

    @property
    def panel_suggest(self):
        return self.layout_manager.panel_suggest

    @property
    def panel_custom(self):
        return self.layout_manager.panel_custom

    @property
    def panel_compare(self):
        return self.layout_manager.panel_compare

    @property
    def panel_data(self):
        return self.layout_manager.panel_data

    @property
    def panel_tiers(self):
        return self.layout_manager.panel_tiers

    @property
    def tabs_visible(self) -> bool:
        return self.layout_manager.tabs_visible

    # =========================================================================
    # DELEGATED CONTROLLER METHODS
    # =========================================================================
    def _perform_boot_sync(self):
        self.controller.start_boot_sync()
        self._start_presence_monitor()
        if not self.configuration.settings.first_run_complete:
            self.root.after(500, self._show_first_run_setup)

    def _perform_deep_sync(self):
        self.controller.execute_deep_sync()

    def _update_loop(self):
        self.controller.update_loop()

    def _schedule_update(self):
        self.controller.schedule_update()

    def _force_reload(self):
        self.controller.force_reload()

    def _refresh_ui_data(self):
        self.controller.refresh_ui_data()

    def _on_dataset_update(self):
        self.controller.on_dataset_update()

    def _background_update_checks(self):
        self.controller.check_background_updates()

    # =========================================================================
    # WINDOW & STATE HANDLING
    # =========================================================================
    def _setup_variables(self):
        """Initializes all bound Tkinter String/Int Vars."""
        self.vars["deck_filter"] = tkinter.StringVar(
            value=self.configuration.settings.deck_filter
        )
        self.vars["set_label"] = tkinter.StringVar(value="")
        self.vars["selected_event"] = tkinter.StringVar(value="")
        self.vars["selected_group"] = tkinter.StringVar(value="")
        self.vars["status_text"] = tkinter.StringVar(value="Ready")

    def update_session_info(self, event_name, draft_id, start_time):
        self.layout_manager.update_session_info(event_name, draft_id, start_time)

    def _ensure_tabs_visible(self):
        self.layout_manager.ensure_tabs_visible()

    def _open_settings(self):
        def _on_settings_changed(key=None):
            s = self.configuration.settings
            if key == "always_on_top" or key is None:
                self.root.attributes("-topmost", s.always_on_top)
            if key == "launch_with_arena" or key is None:
                if s.launch_with_arena:
                    self._start_presence_monitor()
                else:
                    self._stop_presence_monitor()
            if key == "draft_log_enabled" or key is None:
                self.orchestrator.scanner.log_enable(s.draft_log_enabled)
            if (
                key in ["theme", "theme_base", "theme_custom_path", "ui_size"]
                or key is None
            ):
                current_scale = constants.UI_SIZE_DICT.get(s.ui_size, 1.0)
                Theme.apply(
                    self.root,
                    palette=s.theme,
                    engine=getattr(s, "theme_base", "clam"),
                    custom_path=s.theme_custom_path,
                    scale=current_scale,
                )
            if key in ["filter_format"] or key is None:
                self.top_bar.update_deck_filter_options()
            if key in ["result_format", "card_colors_enabled"] or key is None:
                self._refresh_ui_data()
            if (
                key == "arena_log_location"
                and s.arena_log_location
                and os.path.exists(s.arena_log_location)
            ):
                self.orchestrator.set_file_and_scan(s.arena_log_location)
            if (
                key == "database_location"
                and s.database_location
                and os.path.exists(s.database_location)
            ):
                self.orchestrator.scanner.set_data.db_path = s.database_location
                self.orchestrator.scanner.set_data.unknown_id_cache.clear()
                self.orchestrator.request_math_update()
                self._refresh_ui_data()

        parent_window = self.overlay_window if self.overlay_window else self.root
        SettingsWindow(parent_window, self.configuration, _on_settings_changed)

    def _enable_overlay(self):
        if self.overlay_window:
            return
        self.root.withdraw()
        self.overlay_window = CompactOverlay(
            self.root, self, self.configuration, self._disable_overlay
        )
        self._refresh_ui_data()

    def _disable_overlay(self):
        if self.overlay_window:
            self.overlay_window.destroy()
            self.overlay_window = None
        self.root.deiconify()
        current_scale = constants.UI_SIZE_DICT.get(
            self.configuration.settings.ui_size, 1.0
        )
        Theme.apply(
            self.root,
            palette=self.configuration.settings.theme,
            engine=getattr(self.configuration.settings, "theme_base", "clam"),
            custom_path=self.configuration.settings.theme_custom_path,
            scale=current_scale,
        )
        self._refresh_ui_data()

    def _show_first_run_setup(self):
        """Show the one-time, nontechnical Arena integration setup."""
        if self.configuration.settings.first_run_complete:
            return
        try:
            from src.ui.windows.first_run import FirstRunSetupWindow

            FirstRunSetupWindow(
                self.root,
                self.configuration,
                on_complete=self._on_first_run_complete,
            )
        except Exception as e:
            logger.error(f"Could not show first-run setup: {e}", exc_info=True)

    def _on_first_run_complete(self, enabled: bool):
        if enabled:
            self._start_presence_monitor()
        else:
            self._stop_presence_monitor()

    def _start_presence_monitor(self):
        """Keep a lightweight watch on Arena after auto-launch is enabled."""
        if not self.configuration.settings.launch_with_arena:
            return

        # Refresh the startup entry on each normal run.  This repairs stale
        # paths automatically if the application was moved after installation.
        ok, error = set_auto_launch_enabled(True)
        if not ok:
            logger.warning(f"Could not refresh automatic startup registration: {error}")

        if self._presence_monitor is None:
            self._presence_monitor = ArenaPresenceMonitor(
                self.root,
                lambda: self.configuration.settings.arena_log_location,
                self._on_arena_started,
                self._on_arena_stopped,
            )
        self._presence_monitor.start()

    def _stop_presence_monitor(self):
        if self._presence_monitor is not None:
            self._presence_monitor.stop()

    def _on_arena_started(self):
        self._arena_seen = True
        self._user_hidden = False
        self._show_from_background()

    def _on_arena_stopped(self):
        if not self._arena_seen:
            return
        self._user_hidden = False
        self._hide_for_background(user_requested=False)

    def _show_from_background(self):
        try:
            if self.overlay_window and self.overlay_window.winfo_exists():
                self.overlay_window.deiconify()
                self.overlay_window.lift()
            else:
                self.root.deiconify()
                self.root.lift()
        except tkinter.TclError:
            pass

    def _hide_for_background(self, user_requested: bool = False):
        self._user_hidden = bool(user_requested)
        try:
            if self.overlay_window and self.overlay_window.winfo_exists():
                self.overlay_window.withdraw()
            self.root.withdraw()
        except tkinter.TclError:
            pass

    def _on_close(self):
        """Hide to background when auto-launch is active; otherwise quit."""
        if self.configuration.settings.launch_with_arena:
            try:
                self.layout_manager.save_window_state()
                write_configuration(self.configuration)
            except Exception as e:
                logger.error(f"Error saving window state: {e}")
            self._hide_for_background(user_requested=True)
            return

        self._quit()

    def _quit(self):
        """Actually terminate the application, including background monitoring."""
        try:
            self.layout_manager.save_window_state()
            write_configuration(self.configuration)
            self._stop_presence_monitor()
            if hasattr(self, "orchestrator"):
                self.orchestrator.stop()
        except Exception as e:
            logger.error(f"Error during shutdown: {e}")

        self.root.destroy()
        os._exit(0)
