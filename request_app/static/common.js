(() => {
  "use strict";

  const i18nConfig = document.querySelector("#i18n-config");
  const language = i18nConfig?.dataset.language || document.documentElement.lang || "en";
  let translations = {};
  try { translations = JSON.parse(i18nConfig?.dataset.translations || "{}"); } catch (_) { translations = {}; }

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
  const toastElement = document.querySelector("#toast");
  let toastTimer;

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
      throw new Error(tr("Sesja wygasła."));
    }
    if (response.status === 428) {
      if (window.location.pathname !== "/force-password") window.location.assign("/force-password");
      throw new Error(tr("Najpierw zmień hasło tymczasowe."));
    }
    const contentType = response.headers.get("content-type") || "";
    const payload = contentType.includes("application/json") ? await response.json() : null;
    if (!response.ok) {
      const detail = payload?.detail;
      const message = Array.isArray(detail) ? detail.map((item) => item.msg).join(" ") : detail || tr("Błąd serwera ({status}).", { status: response.status });
      throw new Error(message);
    }
    return payload;
  }

  function toast(message, kind = "ok") {
    if (!toastElement) return;
    clearTimeout(toastTimer);
    toastElement.textContent = message;
    toastElement.dataset.kind = kind;
    toastElement.hidden = false;
    toastTimer = setTimeout(() => { toastElement.hidden = true; }, 6500);
  }

  function formatDate(value, withTime = false) {
    if (!value) return tr("brak danych");
    const date = /^\d{4}-\d{2}-\d{2}$/.test(value) ? new Date(`${value}T12:00:00`) : new Date(value);
    if (Number.isNaN(date.getTime())) return value;
    return new Intl.DateTimeFormat(language === "pl" ? "pl-PL" : "en-GB", withTime
      ? { dateStyle: "medium", timeStyle: "short" }
      : { year: "numeric", month: "2-digit", day: "2-digit" }).format(date);
  }

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>'"]/g, (character) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
    }[character]));
  }

  window.RequestUI = { api, toast, formatDate, escapeHtml, csrf, tr, language };

  document.addEventListener("click", (event) => {
    const closeButton = event.target.closest("[data-close-dialog]");
    if (closeButton) closeButton.closest("dialog")?.close();
  });
  document.querySelectorAll("dialog").forEach((dialog) => {
    dialog.addEventListener("click", (event) => { if (event.target === dialog) dialog.close(); });
  });

  const changelogDialog = document.querySelector("#changelog-dialog");
  document.querySelector("[data-open-changelog]")?.addEventListener("click", () => {
    changelogDialog?.showModal();
  });

  const notificationDialog = document.querySelector("#notifications-dialog");
  const notificationList = document.querySelector("#notifications-list");
  const unreadBadge = document.querySelector("[data-unread-count]");
  const markAllButton = document.querySelector("[data-mark-read]");
  const deleteReadButton = document.querySelector("[data-delete-read]");
  let notificationBucket = "unread";

  function renderNotificationCounts(payload) {
    if (unreadBadge) {
      unreadBadge.textContent = payload.unread;
      unreadBadge.hidden = payload.unread === 0;
    }
    document.querySelector('[data-notification-count="unread"]')?.replaceChildren(String(payload.unread));
    document.querySelector('[data-notification-count="read"]')?.replaceChildren(String(payload.read));
  }

  function renderNotifications(payload) {
    renderNotificationCounts(payload);
    if (!notificationList) return;
    notificationList.innerHTML = payload.items.length
      ? payload.items.map((item) => `
        <article class="notification-item ${item.read_at ? "" : "unread"}" data-notification-id="${item.id}">
          <div><strong>${escapeHtml(item.title)}</strong><time>${formatDate(item.created_at, true)}</time></div>
          <p>${escapeHtml(item.body)}</p>
          <div class="notification-item-actions">
            ${item.read_at
              ? `<button class="mini-button danger-text" type="button" data-notification-action="delete" data-id="${item.id}">${tr("Usuń")}</button>`
              : `<button class="mini-button" type="button" data-notification-action="read" data-id="${item.id}">${tr("Oznacz jako przeczytane")}</button>`}
          </div>
        </article>`).join("")
      : `<div class="empty-small">${tr(notificationBucket === "unread" ? "Brak nieodczytanych powiadomień." : "Brak odczytanych powiadomień.")}</div>`;
    markAllButton.hidden = notificationBucket !== "unread" || payload.unread === 0;
    deleteReadButton.hidden = notificationBucket !== "read" || payload.read === 0;
  }

  async function loadNotificationCounts(silent = false) {
    if (!notificationList) return;
    try { renderNotificationCounts(await api("/api/notifications/counts")); }
    catch (error) { if (!silent) toast(error.message, "error"); }
  }

  async function loadNotifications(silent = false) {
    if (!notificationList) return;
    try { renderNotifications(await api(`/api/notifications?bucket=${notificationBucket}`)); }
    catch (error) { if (!silent) toast(error.message, "error"); }
  }

  document.querySelector("[data-open-notifications]")?.addEventListener("click", async () => {
    notificationBucket = "unread";
    document.querySelectorAll("[data-notification-bucket]").forEach((tab) => tab.classList.toggle("active", tab.dataset.notificationBucket === notificationBucket));
    await loadNotifications();
    notificationDialog?.showModal();
  });

  document.querySelectorAll("[data-notification-bucket]").forEach((tab) => {
    tab.addEventListener("click", async () => {
      notificationBucket = tab.dataset.notificationBucket;
      document.querySelectorAll("[data-notification-bucket]").forEach((item) => item.classList.toggle("active", item === tab));
      await loadNotifications();
    });
  });

  notificationList?.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-notification-action]");
    if (!button) return;
    button.disabled = true;
    try {
      if (button.dataset.notificationAction === "read") {
        await api(`/api/notifications/${button.dataset.id}/read`, { method: "POST" });
      } else {
        await api(`/api/notifications/${button.dataset.id}`, { method: "DELETE" });
      }
      await loadNotifications();
    } catch (error) { toast(error.message, "error"); button.disabled = false; }
  });

  markAllButton?.addEventListener("click", async () => {
    try { await api("/api/notifications/read", { method: "POST" }); await loadNotifications(); }
    catch (error) { toast(error.message, "error"); }
  });
  deleteReadButton?.addEventListener("click", async () => {
    try {
      const result = await api("/api/notifications/read/all", { method: "DELETE" });
      toast(tr("Usunięto {count} powiadomień.", { count: result.deleted }));
      await loadNotifications();
    } catch (error) { toast(error.message, "error"); }
  });

  const accountDialog = document.querySelector("#account-dialog");
  const preferencesForm = document.querySelector("#preferences-form");
  document.querySelector("[data-open-account]")?.addEventListener("click", async () => {
    try {
      const preferences = await api("/api/preferences");
      Object.entries(preferences).forEach(([name, enabled]) => {
        const input = preferencesForm?.elements.namedItem(name);
        if (input) input.checked = Boolean(enabled);
      });
      accountDialog?.showModal();
    } catch (error) { toast(error.message, "error"); }
  });

  preferencesForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const message = preferencesForm.querySelector("[data-preferences-message]");
    const values = {
      own_request_liked: preferencesForm.elements.own_request_liked.checked,
      request_changes: preferencesForm.elements.request_changes.checked,
      admin_messages: preferencesForm.elements.admin_messages.checked,
      admin_new_request: preferencesForm.elements.admin_new_request?.checked ?? false,
    };
    try {
      await api("/api/preferences", { method: "PUT", body: values });
      message.textContent = tr("Ustawienia zapisane.");
      message.dataset.kind = "ok";
    } catch (error) { message.textContent = error.message; message.dataset.kind = "error"; }
  });

  const adminViewSwitch = document.querySelector("[data-switch-admin-view]");
  adminViewSwitch?.addEventListener("click", async () => {
    const message = document.querySelector("[data-admin-view-message]");
    adminViewSwitch.disabled = true;
    try {
      await api("/api/account/admin-view", {
        method: "POST",
        body: { mode: adminViewSwitch.dataset.targetMode },
      });
      window.location.assign("/");
    } catch (error) {
      adminViewSwitch.disabled = false;
      if (message) {
        message.textContent = error.message;
        message.dataset.kind = "error";
      }
    }
  });

  const passwordForm = document.querySelector("#password-form");
  passwordForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const message = passwordForm.querySelector("[data-password-message]");
    const body = Object.fromEntries(new FormData(passwordForm).entries());
    if (body.new_password !== body.confirm_password) {
      message.textContent = tr("Wprowadzone hasła nie są identyczne.");
      message.dataset.kind = "error";
      return;
    }
    try {
      const result = await api("/api/account/password", { method: "POST", body });
      passwordForm.reset();
      message.textContent = result.message;
      message.dataset.kind = "ok";
    } catch (error) { message.textContent = error.message; message.dataset.kind = "error"; }
  });

  const NOTIFICATION_SYNC_INTERVAL_MS = 15000;
  let notificationSyncPromise = null;

  function syncNotificationState(forceList = false) {
    if (!notificationList || document.hidden) return Promise.resolve();
    if (notificationSyncPromise) return notificationSyncPromise;
    if (notificationList.querySelector("[data-notification-action]:disabled")) return Promise.resolve();
    const operation = forceList || notificationDialog?.open
      ? loadNotifications(true)
      : loadNotificationCounts(true);
    notificationSyncPromise = Promise.resolve(operation).finally(() => {
      notificationSyncPromise = null;
    });
    return notificationSyncPromise;
  }

  if (notificationList) {
    syncNotificationState();
    window.setInterval(() => syncNotificationState(), NOTIFICATION_SYNC_INTERVAL_MS);
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) syncNotificationState(notificationDialog?.open);
    });
    window.addEventListener("focus", () => syncNotificationState(notificationDialog?.open));
  }
})();
