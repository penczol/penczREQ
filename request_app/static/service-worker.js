"use strict";

const ACTIVATE_UPDATE = "ACTIVATE_UPDATE";

const DEFAULT_BODIES = {
  en: "You have a new notification.",
  pl: "Masz nowe powiadomienie.",
};

function defaultNotification(language = "en") {
  const normalizedLanguage = language === "pl" ? "pl" : "en";
  return {
    title: "penczREQ",
    body: DEFAULT_BODIES[normalizedLanguage],
    url: "/",
    tag: "penczreq-notification",
    language: normalizedLanguage,
  };
}

self.addEventListener("install", () => {
  // The worker handles push only. Authenticated pages, API responses and posters
  // deliberately remain network-only and are never copied to Cache Storage.
  // This also prevents one signed-in account from exposing data to another one.
});

self.addEventListener("message", (event) => {
  if (event.data?.type === ACTIVATE_UPDATE) {
    event.waitUntil(self.skipWaiting());
  }
});

self.addEventListener("push", (event) => {
  let message = defaultNotification();
  if (event.data) {
    try {
      const payload = event.data.json();
      message = { ...defaultNotification(payload?.language), ...payload };
    } catch (_) {
      message = { ...defaultNotification(), body: event.data.text() };
    }
  }

  const target = new URL(message.url || "/", self.location.origin);
  const safeUrl = target.origin === self.location.origin ? target.href : new URL("/", self.location.origin).href;
  event.waitUntil(
    self.registration.showNotification(String(message.title || defaultNotification(message.language).title), {
      body: String(message.body || defaultNotification(message.language).body),
      icon: "/static/icons/pwa-192.png",
      // Android renders `badge` as a monochrome small/status-bar icon. Keep it
      // separate from the full-colour PWA icon and transparent outside the mark.
      badge: "/static/icons/notification-badge-96.png",
      tag: String(message.tag || defaultNotification(message.language).tag),
      data: { url: safeUrl },
    }),
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const targetUrl = event.notification.data?.url || new URL("/", self.location.origin).href;
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then(async (clientList) => {
      for (const client of clientList) {
        if (new URL(client.url).origin === self.location.origin) {
          await client.focus();
          if ("navigate" in client) await client.navigate(targetUrl);
          return;
        }
      }
      if (self.clients.openWindow) await self.clients.openWindow(targetUrl);
    }),
  );
});
