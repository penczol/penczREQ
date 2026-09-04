(() => {
  "use strict";

  const i18nConfig = document.querySelector("#i18n-config");
  const language = i18nConfig?.dataset.language || document.documentElement.lang || "en";
  let translations = {};
  try { translations = JSON.parse(i18nConfig?.dataset.translations || "{}"); } catch (_) { translations = {}; }
  const PASSWORD_PATTERN = "(?=.*[a-z])(?=.*[A-Z])(?=.*[0-9])[\\u0000-\\u007F]{15,128}";
  function tr(source, values = {}) {
    const translated = Object.prototype.hasOwnProperty.call(translations, source);
    if (language === "en" && i18nConfig?.dataset.strict === "1" && !translated) {
      console.error(`Missing English UI translation: ${source}`);
    }
    const template = translated ? translations[source] : source;
    return template.replace(/\{([a-zA-Z0-9_]+)\}/g, (match, key) => (
      Object.prototype.hasOwnProperty.call(values, key) ? String(values[key]) : match
    ));
  }

  const csrf = document.querySelector('meta[name="csrf-token"]')?.content || "";
  const toastElement = document.querySelector("#control-toast");
  let toastTimer;
  let userCache = new Map();
  let throttleCache = [];

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>'"]/g, (character) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
    }[character]));
  }

  function formatDate(value) {
    if (!value) return tr("brak danych");
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return new Intl.DateTimeFormat(language === "pl" ? "pl-PL" : "en-GB", { dateStyle: "medium", timeStyle: "medium" }).format(date);
  }

  function formatBytes(value) {
    const bytes = Number(value) || 0;
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KiB`;
    return `${(bytes / 1024 ** 2).toFixed(1)} MiB`;
  }

  function toast(message, kind = "ok") {
    clearTimeout(toastTimer);
    toastElement.textContent = message;
    toastElement.dataset.kind = kind;
    toastElement.hidden = false;
    toastTimer = setTimeout(() => { toastElement.hidden = true; }, 6500);
  }

  async function api(url, options = {}) {
    const config = { credentials: "same-origin", ...options };
    const method = (config.method || "GET").toUpperCase();
    config.headers = { Accept: "application/json", ...(config.headers || {}) };
    if (method !== "GET" && method !== "HEAD") config.headers["X-CSRF-Token"] = csrf;
    if (config.body && typeof config.body !== "string") {
      config.headers["Content-Type"] = "application/json";
      config.body = JSON.stringify(config.body);
    }
    let response;
    try {
      response = await fetch(url, config);
    } catch (error) {
      if (error?.name === "AbortError") {
        throw new Error(tr("Przekroczono czas oczekiwania na odpowiedź serwera."));
      }
      throw new Error(tr("Nie udało się połączyć z serwerem."));
    }
    if (response.status === 401) {
      window.location.assign("/login");
      throw new Error(tr("Sesja panelu wygasła."));
    }
    if (response.status === 428) {
      window.location.assign("/force-password");
      throw new Error(tr("Najpierw zmień hasło startowe panelu."));
    }
    const contentType = response.headers.get("content-type") || "";
    const payload = contentType.includes("application/json") ? await response.json() : null;
    if (!response.ok) {
      const detail = payload?.detail;
      const message = Array.isArray(detail) ? detail.map((item) => item.msg).join(" ") : detail;
      throw new Error(message || tr("Błąd serwera ({status}).", { status: response.status }));
    }
    return payload;
  }

  function activateTab(name) {
    document.querySelectorAll("[data-control-tab]").forEach((button) => {
      button.classList.toggle("active", button.dataset.controlTab === name);
    });
    document.querySelectorAll("[data-control-section]").forEach((section) => {
      section.classList.toggle("active", section.dataset.controlSection === name);
    });
    window.sessionStorage.setItem("penczreq-control-tab", name);
    if (name === "security") { loadThrottles(); loadEvents(); }
    if (name === "users") loadUsers();
    if (name === "configuration") { loadSettings(); loadSettingsHistory(); }
    if (name === "maintenance") loadBackups();
  }

  document.querySelectorAll("[data-control-tab]").forEach((button) => {
    button.addEventListener("click", () => activateTab(button.dataset.controlTab));
  });

  async function loadOverview() {
    const grid = document.querySelector("#overview-grid");
    try {
      const result = await api("/api/control/overview");
      const integrityOk = Object.values(result.integrity).every((value) => value === "ok");
      grid.innerHTML = `
        <article class="overview-card ${integrityOk ? "good" : "warn"}"><span>${tr("Integralność baz")}</span><strong>${integrityOk ? "OK" : tr("Błąd")}</strong><small>${escapeHtml(Object.entries(result.integrity).map(([key, value]) => `${key}: ${value}`).join(" · "))}</small></article>
        <article class="overview-card ${result.tmdb_configured ? "good" : "warn"}"><span>TMDB</span><strong>${result.tmdb_configured ? tr("Skonfigurowane") : tr("Brak tokenu")}</strong><small>${tr("Klucz jest zapisany zaszyfrowany")}</small></article>
        <article class="overview-card"><span>${tr("Publiczny administrator")}</span><strong>${escapeHtml(result.public_admin?.username || tr("brak"))}</strong><small>${tr("Uprawnienia wynikają z roli, nie z nazwy")}</small></article>
        <article class="overview-card"><span>${tr("Kopie bazy")}</span><strong>${result.backups.length}</strong><small>${result.backups[0] ? tr("Ostatnia: {date}", { date: escapeHtml(formatDate(result.backups[0].created_at)) }) : tr("Brak wykonanych kopii")}</small></article>
        <article class="overview-card"><span>${tr("Publiczny adres")}</span><strong>${escapeHtml(result.public_base_url)}</strong><small>${tr("Konfiguracja aplikacji, nie Caddy")}</small></article>
        <article class="overview-card"><span>${tr("Środowisko")}</span><strong>${escapeHtml(result.environment)}</strong><small>${tr("Wersja {version}", { version: escapeHtml(result.version) })}</small></article>`;
      document.querySelector("[data-bootstrap-warning]").hidden = !result.bootstrap_file_exists;
    } catch (error) {
      grid.innerHTML = `<div class="control-empty">${escapeHtml(error.message)}</div>`;
    }
  }

  function userMarkup(user) {
    const status = user.is_active ? tr("Aktywny") : tr("Zablokowany");
    return `<article class="control-row">
      <div class="control-row-main"><strong>${escapeHtml(user.username)}</strong><span>${tr("ID {id} · utworzono {date}", { id: user.id, date: escapeHtml(formatDate(user.created_at)) })}</span><span>${tr("Ostatnie IP: {ip}", { ip: escapeHtml(user.last_login_ip || tr("brak danych")) })}</span></div>
      <div class="control-row-meta"><span class="state-chip ${user.role === "admin" ? "admin" : ""}">${user.role === "admin" ? tr("Administrator publiczny") : tr("Użytkownik")}</span> <span class="state-chip ${user.is_active ? "" : "blocked"}">${status}</span></div>
      <div class="control-row-actions">
        <button class="control-button" type="button" data-user-action="rename" data-id="${user.id}">${tr("Zmień login")}</button>
        <button class="control-button" type="button" data-user-action="password" data-id="${user.id}">${tr("Reset hasła")}</button>
        <button class="control-button" type="button" data-user-action="sessions" data-id="${user.id}">${tr("Zakończ sesje")}</button>
        ${user.role === "admin" ? "" : `<button class="control-button" type="button" data-user-action="admin" data-id="${user.id}">${tr("Przekaż admina")}</button>`}
        ${user.role === "admin" && user.is_active ? "" : `<button class="control-button ${user.is_active ? "danger" : "success"}" type="button" data-user-action="active" data-id="${user.id}">${user.is_active ? tr("Zablokuj") : tr("Odblokuj")}</button>`}
        ${user.role === "admin"
          ? `<button class="control-button danger protected" type="button" disabled aria-describedby="delete-admin-note-${user.id}">${tr("Usuń użytkownika")}</button><span class="control-action-note" id="delete-admin-note-${user.id}">${tr("Najpierw przekaż rolę administratora innemu aktywnemu kontu.")}</span>`
          : `<button class="control-button danger" type="button" data-user-action="delete" data-id="${user.id}">${tr("Usuń użytkownika")}</button>`}
      </div>
    </article>`;
  }

  async function loadUsers() {
    const list = document.querySelector("#control-users-list");
    try {
      const result = await api("/api/control/users");
      userCache = new Map(result.items.map((user) => [Number(user.id), user]));
      list.innerHTML = result.items.length ? result.items.map(userMarkup).join("") : `<div class="control-empty">${tr("Brak kont.")}</div>`;
    } catch (error) { list.innerHTML = `<div class="control-empty">${escapeHtml(error.message)}</div>`; }
  }

  function actionDialog(title, description, input = null, confirmation = null) {
    const dialog = document.querySelector("#user-action-dialog");
    dialog.querySelector("[data-user-dialog-title]").textContent = title;
    dialog.querySelector("[data-user-dialog-description]").textContent = description;
    const wrap = dialog.querySelector("[data-user-dialog-input-wrap]");
    const field = dialog.querySelector("[data-user-dialog-input]");
    const hasInput = Boolean(input);
    wrap.hidden = !hasInput;
    field.disabled = !hasInput;
    field.required = hasInput;
    field.value = input?.value || "";
    field.type = input?.type || "text";
    for (const [name, value] of Object.entries({
      minlength: input?.minLength,
      maxlength: input?.maxLength,
      pattern: input?.pattern,
      autocomplete: input?.autocomplete,
    })) {
      if (value === undefined || value === null) field.removeAttribute(name);
      else field.setAttribute(name, String(value));
    }
    dialog.querySelector("[data-user-dialog-label]").textContent = input?.label || "";
    const confirmButton = dialog.querySelector("[data-user-dialog-confirm]");
    const destructive = Boolean(confirmation?.destructive);
    confirmButton.textContent = confirmation?.label || tr("Potwierdź");
    confirmButton.classList.toggle("danger", destructive);
    confirmButton.classList.toggle("primary", !destructive);
    return new Promise((resolve) => {
      const listener = () => {
        dialog.removeEventListener("close", listener);
        resolve(dialog.returnValue === "confirm" ? (hasInput ? field.value : true) : null);
      };
      dialog.addEventListener("close", listener);
      dialog.showModal();
      if (hasInput) field.focus();
    });
  }

  document.querySelector("#control-users-list")?.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-user-action]");
    if (!button) return;
    const user = userCache.get(Number(button.dataset.id));
    const reauth = document.querySelector("[data-users-reauth]");
    if (!reauth.value) { toast(tr("Najpierw wpisz hasło Control w polu potwierdzającym."), "error"); reauth.focus(); return; }
    let value = "";
    const action = button.dataset.userAction;
    if (action === "rename") value = await actionDialog(tr("Zmiana loginu"), tr("Nowy login dla konta {username}.", { username: user.username }), { label: tr("Nowy login"), value: user.username, minLength: 3, maxLength: 32, pattern: "[a-z0-9][a-z0-9._-]{2,31}", autocomplete: "off" });
    else if (action === "password") value = await actionDialog(tr("Reset hasła"), tr("Ustaw hasło tymczasowe dla {username}. Użytkownik będzie musiał zmienić je przy następnym logowaniu. Wymagane jest 15–128 znaków ASCII, w tym mała i wielka litera oraz cyfra.", { username: user.username }), { label: tr("Nowe hasło tymczasowe"), type: "password", minLength: 15, maxLength: 128, pattern: PASSWORD_PATTERN, autocomplete: "new-password" });
    else {
      const confirmations = {
        active: {
          title: user.is_active ? tr("Zablokuj użytkownika") : tr("Odblokuj użytkownika"),
          description: user.is_active ? tr("Zablokować konto {username}?", { username: user.username }) : tr("Odblokować konto {username}?", { username: user.username }),
        },
        sessions: {
          title: tr("Zakończ sesje użytkownika"),
          description: tr("Zakończyć wszystkie sesje i subskrypcje push konta {username}?", { username: user.username }),
        },
        admin: {
          title: tr("Przekaż administratora"),
          description: tr("Przekazać rolę administratora publicznego kontu {username}? Wszystkie publiczne sesje zostaną zakończone.", { username: user.username }),
        },
        delete: {
          title: tr("Trwale usuń użytkownika"),
          description: tr("Trwale usunąć konto {username}? Tej operacji nie można cofnąć tak jak blokady. Sesje, subskrypcje push, powiadomienia i aktywne uczestnictwo zostaną usunięte.", { username: user.username }),
          confirmation: { label: tr("Usuń użytkownika"), destructive: true },
        },
      };
      const confirmation = confirmations[action];
      if (!confirmation) return;
      const confirmed = await actionDialog(confirmation.title, confirmation.description, null, confirmation.confirmation);
      if (confirmed === null) return;
    }
    if ((action === "rename" || action === "password") && value === null) return;
    button.disabled = true;
    try {
      const base = `/api/control/users/${user.id}`;
      if (action === "rename") await api(`${base}/username`, { method: "PUT", body: { username: value, current_password: reauth.value } });
      if (action === "password") await api(`${base}/password`, { method: "PUT", body: { temporary_password: value, current_password: reauth.value } });
      if (action === "active") await api(`${base}/active`, { method: "PUT", body: { active: !user.is_active, current_password: reauth.value } });
      if (action === "sessions") await api(`${base}/revoke-sessions`, { method: "POST", body: { current_password: reauth.value } });
      if (action === "admin") await api(`${base}/admin`, { method: "PUT", body: { current_password: reauth.value } });
      if (action === "delete") await api(base, { method: "DELETE", body: { current_password: reauth.value } });
      reauth.value = "";
      const successMessages = {
        rename: tr("Login użytkownika został zmieniony."),
        password: tr("Hasło tymczasowe zostało ustawione. Sesje i subskrypcje użytkownika zakończono."),
        active: user.is_active ? tr("Konto użytkownika zostało zablokowane.") : tr("Konto użytkownika zostało odblokowane."),
        sessions: tr("Sesje i subskrypcje użytkownika zostały zakończone."),
        admin: tr("Rola administratora publicznego została przekazana."),
        delete: tr("Użytkownik został trwale usunięty."),
      };
      toast(successMessages[action] || tr("Operacja została wykonana."));
      await Promise.all([loadUsers(), loadOverview()]);
    } catch (error) { toast(error.message, "error"); }
    finally { button.disabled = false; }
  });

  document.querySelector("#control-create-user")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const button = form.querySelector('[type="submit"]');
    button.disabled = true;
    try {
      await api("/api/control/users", { method: "POST", body: Object.fromEntries(new FormData(form).entries()) });
      form.reset(); toast(tr("Użytkownik został utworzony.")); await loadUsers();
    } catch (error) { toast(error.message, "error"); }
    finally { button.disabled = false; }
  });

  document.querySelector("#control-broadcast")?.addEventListener("submit", async (event) => {
    event.preventDefault(); const form = event.currentTarget;
    try {
      const result = await api("/api/control/broadcast", { method: "POST", body: Object.fromEntries(new FormData(form).entries()) });
      form.reset(); toast(tr("Powiadomienie wysłano do {count} kont.", { count: result.recipients }));
    } catch (error) { toast(error.message, "error"); }
  });

  function throttleMarkup(item, index) {
    const blocked = item.blocked_until && new Date(item.blocked_until) > new Date();
    const source = item.source === "control" ? "Control" : tr("Publiczny");
    const kind = item.scope === "ip" ? tr("adres IP / podsieć") : tr("login");
    return `<article class="control-row">
      <div class="control-row-main"><strong>${source} · ${kind}: ${escapeHtml(item.display_key)}</strong><span>${tr("Ostatnia próba: {date}", { date: escapeHtml(formatDate(item.last_failure_at)) })}</span></div>
      <div class="control-row-meta"><span class="state-chip ${blocked ? "blocked" : ""}">${blocked ? tr("Blokada do {date}", { date: escapeHtml(formatDate(item.blocked_until)) }) : tr("{count} prób w oknie", { count: item.failures })}</span></div>
      <div class="control-row-actions"><button class="control-button danger" type="button" data-reset-throttle="${index}">${tr("Wyczyść wpis")}</button></div>
    </article>`;
  }

  async function loadThrottles() {
    const list = document.querySelector("#throttle-list");
    try {
      const result = await api("/api/control/throttles"); throttleCache = result.items;
      list.innerHTML = throttleCache.length ? throttleCache.map(throttleMarkup).join("") : `<div class="control-empty">${tr("Brak aktywnych i ostatnich wpisów ochrony.")}</div>`;
    } catch (error) { list.innerHTML = `<div class="control-empty">${escapeHtml(error.message)}</div>`; }
  }

  document.querySelector("#throttle-list")?.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-reset-throttle]"); if (!button) return;
    const password = document.querySelector("[data-security-reauth]");
    if (!password.value) { toast(tr("Wpisz hasło Control."), "error"); password.focus(); return; }
    const item = throttleCache[Number(button.dataset.resetThrottle)];
    const confirmed = await actionDialog(tr("Wyczyść ochronę"), tr("Usunąć wpis {key}?", { key: item.display_key }));
    if (confirmed === null) return;
    try {
      await api("/api/control/throttles/reset", { method: "POST", body: { source: item.source, scope: item.scope, key: item.key, current_password: password.value } });
      password.value = ""; toast(tr("Wpis ochrony został usunięty.")); await loadThrottles();
    } catch (error) { toast(error.message, "error"); }
  });

  async function loadEvents() {
    const list = document.querySelector("#event-list");
    try {
      const result = await api("/api/control/events");
      list.innerHTML = result.items.length ? result.items.map((item) => `<article class="event-row ${escapeHtml(item.severity)}"><time>${escapeHtml(formatDate(item.occurred_at))}</time><strong>${escapeHtml(item.event_type)}</strong><div><span>${escapeHtml(item.username || item.actor_type)} · ${escapeHtml(item.ip_address || tr("bez IP"))}</span><small>${escapeHtml(JSON.stringify(item.details || {}))}</small></div></article>`).join("") : `<div class="control-empty">${tr("Brak zdarzeń.")}</div>`;
    } catch (error) { list.innerHTML = `<div class="control-empty">${escapeHtml(error.message)}</div>`; }
  }

  async function loadSettings() {
    const form = document.querySelector("#control-settings-form");
    try {
      const result = await api("/api/control/settings");
      form.elements.public_base_url.value = result.public_base_url;
      form.elements.known_proxies.value = result.known_proxies;
      form.elements.security_log_retention_days.value = result.security_log_retention_days;
      form.elements.backup_retention_days.value = result.backup_retention_days;
      document.querySelector("[data-tmdb-state]").textContent = tr(result.tmdb_configured ? "Token jest skonfigurowany i zapisany zaszyfrowany." : "Brak tokenu TMDB.");
    } catch (error) { toast(error.message, "error"); }
  }

  document.querySelector("#control-settings-form")?.addEventListener("submit", async (event) => {
    event.preventDefault(); const form = event.currentTarget; const button = form.querySelector('[type="submit"]'); button.disabled = true;
    const values = Object.fromEntries(new FormData(form).entries());
    values.security_log_retention_days = Number(values.security_log_retention_days);
    values.backup_retention_days = Number(values.backup_retention_days);
    try {
      await api("/api/control/settings", { method: "PUT", body: values });
      form.elements.current_password.value = ""; form.elements.tmdb_token.value = "";
      toast(tr("Konfiguracja została zapisana i zapisana w historii.")); await Promise.all([loadSettings(), loadOverview(), loadSettingsHistory()]);
    } catch (error) { toast(error.message, "error"); }
    finally { button.disabled = false; }
  });

  document.querySelector("[data-test-tmdb]")?.addEventListener("click", async (event) => {
    const form = document.querySelector("#control-settings-form");
    if (!form.elements.current_password.value) { toast(tr("Wpisz hasło Control w sekcji zatwierdzenia."), "error"); return; }
    event.currentTarget.disabled = true;
    try {
      const result = await api("/api/control/test-tmdb", { method: "POST", body: { tmdb_token: form.elements.tmdb_token.value, current_password: form.elements.current_password.value } });
      toast(tr("Połączenie z TMDB działa. Wyniki testowe: {count}.", { count: result.results }));
    } catch (error) { toast(error.message, "error"); }
    finally { event.currentTarget.disabled = false; }
  });

  async function loadSettingsHistory() {
    const list = document.querySelector("#settings-history-list");
    try {
      const result = await api("/api/control/settings-history");
      list.innerHTML = result.items.length ? result.items.map((item) => {
        const previousValue = item.previous_value === null || item.previous_value === undefined ? tr("brak") : item.previous_value;
        const newValue = item.new_value === null || item.new_value === undefined ? tr("brak") : item.new_value;
        return `<article class="event-row"><time>${escapeHtml(formatDate(item.changed_at))}</time><strong>${escapeHtml(item.key)}</strong><div><span>${escapeHtml(item.changed_by)}</span><small>${escapeHtml(previousValue)} → ${escapeHtml(newValue)}</small></div></article>`;
      }).join("") : `<div class="control-empty">${tr("Brak zmian.")}</div>`;
    } catch (error) { list.innerHTML = `<div class="control-empty">${escapeHtml(error.message)}</div>`; }
  }

  async function loadBackups() {
    const list = document.querySelector("#backup-list");
    try {
      const result = await api("/api/control/backups");
      list.innerHTML = result.items.length ? result.items.map((item) => `<article class="control-row"><div class="control-row-main"><strong>${escapeHtml(item.name)}</strong><span>${escapeHtml(formatDate(item.created_at))}</span></div><div class="control-row-meta">${escapeHtml(formatBytes(item.size))}</div><div></div></article>`).join("") : `<div class="control-empty">${tr("Brak kopii.")}</div>`;
    } catch (error) { list.innerHTML = `<div class="control-empty">${escapeHtml(error.message)}</div>`; }
  }

  document.querySelector("[data-create-backup]")?.addEventListener("click", async (event) => {
    const password = document.querySelector("[data-backup-reauth]");
    if (!password.value) { toast(tr("Wpisz hasło Control."), "error"); password.focus(); return; }
    event.currentTarget.disabled = true;
    try {
      await api("/api/control/backup", { method: "POST", body: { current_password: password.value } });
      password.value = ""; toast(tr("Spójna kopia obu baz została utworzona.")); await Promise.all([loadBackups(), loadOverview()]);
    } catch (error) { toast(error.message, "error"); }
    finally { event.currentTarget.disabled = false; }
  });

  document.querySelector("#control-username-form")?.addEventListener("submit", async (event) => {
    event.preventDefault(); const form = event.currentTarget;
    try { await api("/api/control/account/username", { method: "PUT", body: Object.fromEntries(new FormData(form).entries()) }); toast(tr("Login panelu został zmieniony.")); form.elements.current_password.value = ""; }
    catch (error) { toast(error.message, "error"); }
  });

  document.querySelector("#control-password-form")?.addEventListener("submit", async (event) => {
    event.preventDefault(); const form = event.currentTarget; const body = Object.fromEntries(new FormData(form).entries());
    if (body.new_password !== body.confirm_password) { toast(tr("Wprowadzone hasła nie są identyczne."), "error"); return; }
    try { await api("/api/control/account/password", { method: "PUT", body }); window.location.assign("/login"); }
    catch (error) { toast(error.message, "error"); }
  });

  document.querySelector("[data-load-users]")?.addEventListener("click", loadUsers);
  document.querySelector("[data-load-throttles]")?.addEventListener("click", loadThrottles);
  document.querySelector("[data-load-events]")?.addEventListener("click", loadEvents);
  document.querySelector("[data-load-settings-history]")?.addEventListener("click", loadSettingsHistory);
  document.querySelector("[data-load-backups]")?.addEventListener("click", loadBackups);
  document.querySelector("[data-refresh-all]")?.addEventListener("click", loadOverview);

  const initialTab = window.sessionStorage.getItem("penczreq-control-tab") || "overview";
  activateTab(document.querySelector(`[data-control-tab="${initialTab}"]`) ? initialTab : "overview");
  loadOverview();
})();
