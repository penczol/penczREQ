(() => {
  "use strict";

  if (window.__penczreqPwaInitialized) return;
  window.__penczreqPwaInitialized = true;

  const api = window.RequestUI?.api;
  const toast = window.RequestUI?.toast;
  const tr = window.RequestUI?.tr || ((source) => source);
  const installButton = document.querySelector("[data-install-pwa]");
  const enableButton = document.querySelector("[data-enable-push]");
  const disableButton = document.querySelector("[data-disable-push]");
  const testButton = document.querySelector("[data-test-push]");
  const pushMessage = document.querySelector("[data-push-message]");
  const accountButton = document.querySelector("[data-open-account]");
  const updateBanner = document.querySelector("[data-pwa-update]");
  const applyUpdateButton = document.querySelector("[data-apply-pwa-update]");
  const dismissUpdateButton = document.querySelector("[data-dismiss-pwa-update]");
  const authenticated = document.body.classList.contains("admin-view") || document.body.classList.contains("user-view");
  const WORKER_UPDATE_INTERVAL_MS = 60 * 60 * 1000;
  let deferredInstallPrompt = null;
  let workerRegistration = null;
  let workerRegistrationPromise = null;
  let updateReloadStarted = false;

  function isStandalone() {
    return window.matchMedia("(display-mode: standalone)").matches
      || window.navigator.standalone === true;
  }

  function setMessage(message, kind = "") {
    if (!pushMessage) return;
    pushMessage.textContent = message;
    pushMessage.dataset.kind = kind;
  }

  function base64UrlToBytes(value) {
    const padding = "=".repeat((4 - (value.length % 4)) % 4);
    const normalized = (value + padding).replace(/-/g, "+").replace(/_/g, "/");
    const raw = window.atob(normalized);
    return Uint8Array.from(raw, (character) => character.charCodeAt(0));
  }

  function updateInstallButton() {
    if (!installButton) return;
    installButton.hidden = isStandalone() || !window.isSecureContext;
  }

  function showWorkerUpdate() {
    if (updateBanner) updateBanner.hidden = false;
  }

  function watchWorkerRegistration(registration) {
    if (registration.waiting && navigator.serviceWorker.controller) showWorkerUpdate();
    registration.addEventListener("updatefound", () => {
      const candidate = registration.installing;
      if (!candidate) return;
      candidate.addEventListener("statechange", () => {
        if (candidate.state === "installed" && navigator.serviceWorker.controller) {
          showWorkerUpdate();
        }
      });
    });
    const checkForUpdate = () => registration.update().catch((error) => {
      console.warn("penczREQ service worker update check failed.", error);
    });
    window.setTimeout(checkForUpdate, 0);
    window.setInterval(checkForUpdate, WORKER_UPDATE_INTERVAL_MS);
  }

  async function registerWorker() {
    if (workerRegistration) return workerRegistration;
    if (!window.isSecureContext || !("serviceWorker" in navigator)) return null;
    if (!workerRegistrationPromise) {
      workerRegistrationPromise = navigator.serviceWorker.register("/service-worker.js", {
        scope: "/",
        updateViaCache: "none",
      }).then((registration) => {
        workerRegistration = registration;
        watchWorkerRegistration(registration);
        return registration;
      }).catch((error) => {
        workerRegistrationPromise = null;
        throw error;
      });
    }
    workerRegistration = await workerRegistrationPromise;
    return workerRegistration;
  }

  async function currentSubscription() {
    const registration = await registerWorker();
    return registration ? registration.pushManager.getSubscription() : null;
  }

  async function saveSubscription(subscription) {
    if (!api || !subscription) return;
    await api("/api/push/subscription", {
      method: "PUT",
      body: subscription.toJSON(),
    });
  }

  async function updatePushState() {
    if (!pushMessage) return;
    if (!window.isSecureContext) {
      setMessage(tr("Powiadomienia systemowe będą dostępne po uruchomieniu strony przez HTTPS."));
      enableButton.hidden = true;
      disableButton.hidden = true;
      testButton.hidden = true;
      return;
    }
    if (!("serviceWorker" in navigator) || !("PushManager" in window) || !("Notification" in window)) {
      setMessage(tr("Ta przeglądarka nie obsługuje powiadomień Web Push."), "error");
      enableButton.hidden = true;
      disableButton.hidden = true;
      testButton.hidden = true;
      return;
    }
    const subscription = await currentSubscription();
    const active = Notification.permission === "granted" && Boolean(subscription);
    enableButton.hidden = active || Notification.permission === "denied";
    disableButton.hidden = !subscription;
    testButton.hidden = !active;
    if (active) {
      setMessage(tr("Powiadomienia systemowe są włączone na tym urządzeniu."), "ok");
    } else if (Notification.permission === "denied") {
      setMessage(tr("Powiadomienia są zablokowane w ustawieniach przeglądarki."), "error");
    } else {
      setMessage(tr("Powiadomienia systemowe nie są jeszcze włączone na tym urządzeniu."));
    }
  }

  window.addEventListener("beforeinstallprompt", (event) => {
    event.preventDefault();
    deferredInstallPrompt = event;
    if (installButton && !isStandalone()) installButton.hidden = false;
  });

  window.addEventListener("appinstalled", () => {
    deferredInstallPrompt = null;
    if (installButton) installButton.hidden = true;
    toast?.(tr("Aplikacja penczREQ została zainstalowana."));
  });

  installButton?.addEventListener("click", async () => {
    if (!deferredInstallPrompt) {
      toast?.(tr("Instalację znajdziesz również w menu przeglądarki."), "error");
      return;
    }
    await deferredInstallPrompt.prompt();
    const choice = await deferredInstallPrompt.userChoice;
    deferredInstallPrompt = null;
    installButton.hidden = choice.outcome === "accepted";
  });

  applyUpdateButton?.addEventListener("click", async () => {
    applyUpdateButton.disabled = true;
    try {
      const registration = await registerWorker();
      if (!registration?.waiting) {
        window.location.reload();
        return;
      }
      registration.waiting.postMessage({ type: "ACTIVATE_UPDATE" });
    } catch (error) {
      console.error("penczREQ service worker update failed.", error);
      toast?.(tr("Nie udało się uruchomić obsługi aplikacji na tym urządzeniu."), "error");
      applyUpdateButton.disabled = false;
    }
  });

  dismissUpdateButton?.addEventListener("click", () => {
    if (updateBanner) updateBanner.hidden = true;
  });

  navigator.serviceWorker?.addEventListener("controllerchange", () => {
    // Activation is possible only after a user accepts the waiting worker.
    // Reload every controlled tab once so no tab keeps an older UI bundle.
    if (updateReloadStarted) return;
    updateReloadStarted = true;
    window.location.reload();
  });

  enableButton?.addEventListener("click", async () => {
    enableButton.disabled = true;
    try {
      const permission = await Notification.requestPermission();
      if (permission !== "granted") {
        await updatePushState();
        return;
      }
      const config = await api("/api/push/config");
      const registration = await registerWorker();
      let subscription = await registration.pushManager.getSubscription();
      if (!subscription) {
        subscription = await registration.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: base64UrlToBytes(config.public_key),
        });
      }
      await saveSubscription(subscription);
      await api("/api/push/test", { method: "POST" });
      toast?.(tr("Powiadomienia włączone. Wysyłam wiadomość testową."));
      await updatePushState();
    } catch (error) {
      setMessage(error.message || tr("Nie udało się włączyć powiadomień."), "error");
    } finally {
      enableButton.disabled = false;
    }
  });

  disableButton?.addEventListener("click", async () => {
    disableButton.disabled = true;
    try {
      const subscription = await currentSubscription();
      if (subscription) {
        await api("/api/push/subscription", {
          method: "DELETE",
          body: { endpoint: subscription.endpoint },
        });
        await subscription.unsubscribe();
      }
      toast?.(tr("Powiadomienia systemowe wyłączone na tym urządzeniu."));
      await updatePushState();
    } catch (error) {
      setMessage(error.message || tr("Nie udało się wyłączyć powiadomień."), "error");
    } finally {
      disableButton.disabled = false;
    }
  });

  testButton?.addEventListener("click", async () => {
    testButton.disabled = true;
    try {
      await api("/api/push/test", { method: "POST" });
      toast?.(tr("Wiadomość testowa została zlecona."));
    } catch (error) {
      setMessage(error.message || tr("Nie udało się wysłać testu."), "error");
    } finally {
      testButton.disabled = false;
    }
  });

  accountButton?.addEventListener("click", () => {
    updatePushState().catch((error) => setMessage(error.message, "error"));
  });

  updateInstallButton();

  registerWorker()
    .then(async () => {
      if (!authenticated || !api) return;
      const subscription = await currentSubscription();
      if (subscription && Notification.permission === "granted") {
        await saveSubscription(subscription);
      }
    })
    .catch((error) => {
      console.error("penczREQ service worker registration failed.", error);
      setMessage(tr("Nie udało się uruchomić obsługi aplikacji na tym urządzeniu."), "error");
    });
})();
