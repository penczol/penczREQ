(() => {
  "use strict";

  const { api, toast, formatDate, escapeHtml, tr, language } = window.RequestUI;
  const isAdmin = document.querySelector("#request-app-config")?.dataset.isAdmin === "1";
  const list = document.querySelector("#request-list");
  const note = document.querySelector("[data-section-note]");
  const toolbar = document.querySelector("[data-active-toolbar]");
  const statusFilter = document.querySelector("[data-status-filter]");
  const sortSelect = document.querySelector("[data-sort]");
  const statusControl = document.querySelector("[data-status-control]");
  const requestPagination = document.querySelector("[data-request-pagination]");
  const pageSizeSelect = document.querySelector("[data-page-size]");
  const pageSummaryDesktop = document.querySelector("[data-page-summary-desktop]");
  const pageSummaryMobile = document.querySelector("[data-page-summary-mobile]");
  const searchDialog = document.querySelector("#search-dialog");
  const searchForm = document.querySelector("#search-form");
  const searchResults = document.querySelector("#search-results");
  const deleteDialog = document.querySelector("#delete-dialog");
  const deleteForm = document.querySelector("#delete-form");
  const states = { active: [], upcoming: [], completed: [] };
  let currentState = "active";
  let deleteTarget = null;
  const activeSeasonByGroup = new Map();
  let stateFingerprint = "";
  let requestLoadPromise = null;
  const pageByState = { active: 1, upcoming: 1, completed: 1 };
  const paginationByState = Object.fromEntries(Object.keys(states).map((state) => [state, {
    page: 1, page_size: 25, total_items: 0, total_all_items: 0, total_pages: 1,
  }]));

  function normalizeRequestResult(state, result) {
    if (!result || typeof result !== "object" || !Array.isArray(result.items)) {
      throw new Error(tr("Nieprawidłowy kontrakt listy dla karty „{state}”.", { state }));
    }
    const pagination = result.pagination;
    if (!pagination || typeof pagination !== "object") {
      throw new Error(tr("Backend nie zwrócił paginacji dla karty „{state}”. Uruchom ponownie Public DEV.", { state }));
    }
    const fields = ["page", "page_size", "total_items", "total_all_items", "total_pages"];
    if (fields.some((field) => !Number.isInteger(pagination[field]))) {
      throw new Error(tr("Backend zwrócił niepełną paginację dla karty „{state}”.", { state }));
    }
    if (![25, 50, 100].includes(pagination.page_size)
        || pagination.page < 1
        || pagination.total_items < 0
        || pagination.total_all_items < pagination.total_items
        || pagination.total_pages < 1
        || pagination.page > pagination.total_pages
        || pagination.total_pages !== Math.max(1, Math.ceil(pagination.total_items / pagination.page_size))) {
      throw new Error(tr("Backend zwrócił niespójną paginację dla karty „{state}”.", { state }));
    }
    return {
      items: result.items,
      pagination: {
        page: pagination.page,
        page_size: pagination.page_size,
        total_items: pagination.total_items,
        total_all_items: pagination.total_all_items,
        total_pages: pagination.total_pages,
      },
    };
  }

  const statusLabels = {
    pending: tr("Oczekujący"),
    translation: tr("W oczekiwaniu na premierę Blu-ray/VOD"),
    in_progress: tr("W trakcie realizacji"),
    missing: tr("Aktualnie brak źródła"),
  };
  const notes = {
    active: tr("Pozycje gotowe do realizacji. Użytkownicy widzą requesty anonimowo."),
    upcoming: tr("Po dacie premiery pozycja automatycznie trafi do głównej listy, a zainteresowani dostaną powiadomienie."),
    completed: tr("Archiwum zrealizowanych próśb."),
  };

  function cleanTitle(value) {
    return String(value || "").trim().normalize("NFC");
  }

  function primaryTitle(item) {
    const localized = language === "pl" ? item.title_pl : item.title_en;
    return cleanTitle(localized) || cleanTitle(item.title_original) || "—";
  }

  function secondaryTitle(item) {
    const original = cleanTitle(item.title_original);
    return original && original !== cleanTitle(primaryTitle(item)) ? original : "";
  }

  function posterMarkup(item) {
    const alt = escapeHtml(tr("Okładka: {title}", { title: primaryTitle(item) }));
    return item.poster_path
      ? `<img class="poster" src="/posters/${encodeURIComponent(item.poster_path)}" alt="${alt}" loading="lazy">`
      : `<div class="poster poster-empty" aria-label="${tr("Brak okładki")}"><span>${tr("Brak\nokładki").replace("\n", "<br>")}</span></div>`;
  }

  function statusMarkup(item) {
    if (currentState === "upcoming") return `<div class="release-box"><span>${tr("Premiera bazowa")}</span><strong>${formatDate(item.release_date)}</strong></div>`;
    if (currentState === "completed") return `<span class="status-chip completed">${tr("Zrealizowany")}</span>`;
    if (!isAdmin) return `<span class="status-chip ${item.status}">${escapeHtml(statusLabels[item.status] || item.status)}</span>`;
    const options = Object.entries(statusLabels).map(([value, label]) =>
      `<option value="${value}" ${item.status === value ? "selected" : ""}>${escapeHtml(label)}</option>`
    ).join("");
    return `<div class="status-admin-wrap">
      <label class="status-control"><span>${tr("Status")}</span><select data-status-select data-id="${item.id}" data-current="${item.status}">${options}</select></label>
      <button class="mini-button confirm-status" type="button" data-action="confirm-status" data-id="${item.id}" hidden>${tr("Zatwierdź zmianę")}</button>
    </div>`;
  }

  function adminMeta(item) {
    if (!isAdmin) return "";
    const likers = item.likers || [];
    return `<div class="admin-meta">
      <span>${tr("Dodał:")} <strong>${escapeHtml(item.requester_username || "—")}</strong></span>
      <details><summary>${tr("Polubili ({count})", { count: likers.length })}</summary><div>${likers.length ? likers.map(escapeHtml).join(", ") : tr("Brak")}</div></details>
    </div>`;
  }

  function availability(value) { return value ? formatDate(value) : tr("brak danych"); }

  function combinedAvailability(digital, physical) {
    if (!digital && !physical) return tr("brak danych");
    const parts = [];
    if (digital) parts.push(`VOD: ${formatDate(digital)}`);
    if (physical) parts.push(tr("fizyczna: {date}", { date: formatDate(physical) }));
    return parts.join(" · ");
  }

  function releaseDatesMarkup(item) {
    const rows = [
      `<span><small>${tr("Świat · kino")}</small>${availability(item.world_theatrical_date)}</span>`,
      `<span><small>${tr("Świat · Blu-ray/VOD")}</small>${combinedAvailability(item.world_digital_date, item.world_physical_date)}</span>`,
    ];
    if (language === "pl") {
      rows.splice(1, 0, `<span><small>${tr("Polska · kino")}</small>${availability(item.pl_theatrical_date)}</span>`);
      rows.push(`<span><small>${tr("Polska · Blu-ray/VOD")}</small>${combinedAvailability(item.pl_digital_date, item.pl_physical_date)}</span>`);
    }
    return `<div class="release-dates-grid">${rows.join("")}</div>`;
  }

  function likeMarkup(item) {
    if (item.author_like) {
      return `<button class="like-button liked locked" type="button" disabled title="${tr("Automatyczny lajk autora requestu")}"><span aria-hidden="true">♥</span> <span data-like-count>${item.like_count}</span></button>`;
    }
    if (item.liked_by_me && !item.can_unlike) {
      return `<button class="like-button liked locked" type="button" disabled title="${tr("Minęło 10 sekund na wycofanie lajka")}"><span aria-hidden="true">♥</span> <span data-like-count>${item.like_count}</span></button>`;
    }
    const undo = item.liked_by_me ? `<span class="like-undo" data-like-undo>${tr("Cofnij · {seconds}s", { seconds: 10 })}</span>` : "";
    return `<button class="like-button ${item.liked_by_me ? "liked removable" : ""}" type="button" data-action="like" data-id="${item.id}" data-deadline="${item.like_removal_deadline || ""}" aria-pressed="${item.liked_by_me}"><span aria-hidden="true">♥</span> <span data-like-count>${item.like_count}</span>${undo}</button>`;
  }

  function serviceLink(service, href, label) {
    return `<a class="service-link ${service}" href="${escapeHtml(href)}" target="_blank" rel="noopener" title="${escapeHtml(label)}" aria-label="${escapeHtml(label)}"><img src="/static/icons/${service}.svg" alt="" aria-hidden="true"></a>`;
  }

  function actionMarkup(item) {
    const tmdbType = item.media_type === "movie" ? "movie" : "tv";
    const tmdbUrl = `https://www.themoviedb.org/${tmdbType}/${item.tmdb_id}`;
    let external = serviceLink("tmdb", tmdbUrl, tr("Otwórz pozycję w TMDB"));
    if (item.imdb_id) {
      external += serviceLink("imdb", `https://www.imdb.com/title/${encodeURIComponent(item.imdb_id)}/`, tr("Otwórz pozycję w IMDb"));
    }
    if (isAdmin) {
      external += `<button class="mini-button id-copy-button" type="button" data-action="copy" data-copy-value="${escapeHtml(String(item.tmdb_id))}"><span class="copy-label"><span class="copy-prefix">${tr("Kopiuj")}</span> TMDB ID</span></button>`;
      external += item.imdb_id
        ? `<button class="mini-button id-copy-button" type="button" data-action="copy" data-copy-value="${escapeHtml(item.imdb_id)}"><span class="copy-label"><span class="copy-prefix">${tr("Kopiuj")}</span> IMDb ID</span></button>`
        : `<span class="mini-button disabled">${tr("Brak IMDb ID")}</span>`;
    }
    let adminActions = "";
    if (isAdmin && currentState === "active") {
      adminActions = `<span class="complete-control"><button class="button success" type="button" data-action="prepare-complete" data-id="${item.id}"><span class="complete-label-desktop">${tr("Oznacz jako zrealizowany")}</span><span class="complete-label-mobile">${tr("Wypełniony")}</span></button></span>
        <button class="button danger ghost-danger" type="button" data-action="delete" data-id="${item.id}" data-title="${escapeHtml(primaryTitle(item))}">${tr("Usuń")}</button>`;
    } else if (isAdmin && currentState === "completed") {
      adminActions = `<button class="button" type="button" data-action="restore" data-id="${item.id}">${tr("Przywróć do requestów")}</button>`;
    }
    const withdrawAction = item.can_withdraw
      ? `<button class="button danger ghost-danger" type="button" data-action="withdraw" data-id="${item.id}">${tr("Wycofaj mój request")}</button>`
      : "";
    const stateActions = `${withdrawAction}${adminActions}`;
    return `<div class="card-actions">
      <div class="external-actions">${external}</div>
      <div class="request-actions">
        <div class="participation-actions">${likeMarkup(item)}</div>
        ${stateActions ? `<div class="request-state-actions">${stateActions}</div>` : ""}
      </div>
    </div>`;
  }

  function releasePeriodLabel(item) {
    if (item.media_type === "movie") return item.release_year || tr("Rok nieznany");
    const start = item.series_start_year || "????";
    if (item.series_status === "ongoing") return `${start}-${tr("trwa")}`;
    if (item.series_status === "ended") return `${start}-${item.series_end_year || "????"}`;
    return `${start}-????`;
  }

  function cardMarkup(item) {
    const typeLabel = item.media_type === "movie" ? tr("Film") : tr("Serial");
    const secondary = secondaryTitle(item);
    const original = secondary ? `<p class="original-title">${escapeHtml(secondary)}</p>` : "";
    return `<article class="request-card request-card-v02" data-request-id="${item.id}">
      ${posterMarkup(item)}
      <div class="card-content">
        <div class="card-heading"><div><div class="card-kickers"><span>${typeLabel}</span><span>${releasePeriodLabel(item)}</span></div><h2>${escapeHtml(primaryTitle(item))}</h2>${original}</div>${statusMarkup(item)}</div>
        <div class="card-meta-row">
          <div class="card-facts"><span><small>${tr("Dodano")}</small>${formatDate(item.created_at)}</span>${currentState === "completed" ? `<span><small>${tr("Zrealizowano")}</small>${formatDate(item.completed_at)}</span>` : ""}</div>
          ${adminMeta(item)}
        </div>
        ${releaseDatesMarkup(item)}
        ${actionMarkup(item)}
      </div>
    </article>`;
  }

  function sortedAndFiltered() {
    let items = [...states[currentState]];
    const sort = sortSelect.value;
    const statusOrder = { in_progress: 0, pending: 1, translation: 2, missing: 3 };
    items.sort((a, b) => {
      if (sort === "oldest") return String(a.created_at).localeCompare(String(b.created_at));
      if (sort === "likes_desc") return b.like_count - a.like_count || String(b.created_at).localeCompare(String(a.created_at));
      if (sort === "likes_asc") return a.like_count - b.like_count || String(b.created_at).localeCompare(String(a.created_at));
      if (sort === "status") return (statusOrder[a.status] ?? 9) - (statusOrder[b.status] ?? 9) || String(b.created_at).localeCompare(String(a.created_at));
      return String(b.created_at).localeCompare(String(a.created_at));
    });
    return items;
  }

  function updateLikeCountdowns() {
    document.querySelectorAll(".like-button.removable[data-deadline]").forEach((button) => {
      const remaining = new Date(button.dataset.deadline).getTime() - Date.now();
      const label = button.querySelector("[data-like-undo]");
      if (remaining <= 0) {
        button.disabled = true;
        button.classList.remove("removable");
        button.classList.add("locked");
        button.title = tr("Minęło 10 sekund na wycofanie lajka");
        label?.remove();
      } else if (label) label.textContent = tr("Cofnij · {seconds}s", { seconds: Math.max(1, Math.ceil(remaining / 1000)) });
    });
  }

  function seasonGroupKey(item) {
    return `${currentState}:tv:${item.tmdb_id}`;
  }

  function groupSortedItems(items) {
    const groups = [];
    const tvGroups = new Map();
    items.forEach((item) => {
      if (item.media_type !== "tv") {
        groups.push({ key: `${currentState}:request:${item.id}`, items: [item] });
        return;
      }
      const key = seasonGroupKey(item);
      let group = tvGroups.get(key);
      if (!group) {
        group = { key, items: [] };
        tvGroups.set(key, group);
        groups.push(group);
      }
      group.items.push(item);
    });
    groups.forEach((group) => {
      if (group.items.length > 1) {
        group.items.sort((left, right) => (Number(left.season_number) || 0) - (Number(right.season_number) || 0));
      }
    });
    return groups;
  }

  function activeItemForGroup(group) {
    if (group.items.length === 1) return group.items[0];
    const rememberedId = activeSeasonByGroup.get(group.key);
    const activeItem = group.items.find((item) => item.id === rememberedId) || group.items[0];
    activeSeasonByGroup.set(group.key, activeItem.id);
    return activeItem;
  }

  function seasonTabsMarkup(group, activeItem) {
    return `<div class="season-tabs" role="tablist" aria-label="${tr("Zamówione sezony")}">${group.items.map((item) => {
      const seasonNumber = Number(item.season_number);
      const hasSeasonNumber = Number.isFinite(seasonNumber);
      const season = hasSeasonNumber ? seasonNumber : "—";
      const mobileSeason = hasSeasonNumber ? String(seasonNumber).padStart(2, "0") : "--";
      const active = item.id === activeItem.id;
      return `<button class="season-tab ${active ? "active" : ""}" type="button" role="tab" aria-selected="${active}" tabindex="${active ? "0" : "-1"}" data-action="switch-season" data-request-id="${item.id}"><span class="season-tab-desktop">${tr("Sezon {season}", { season })}</span><span class="season-tab-mobile">S${mobileSeason}</span></button>`;
    }).join("")}</div>`;
  }

  function groupCardMarkup(group) {
    const activeItem = activeItemForGroup(group);
    if (activeItem.media_type !== "tv") return cardMarkup(activeItem);
    return `<div class="request-season-group">${seasonTabsMarkup(group, activeItem)}${cardMarkup(activeItem)}</div>`;
  }

  function render() {
    const items = sortedAndFiltered();
    const groups = groupSortedItems(items);
    const pagination = paginationByState[currentState];
    toolbar.hidden = currentState === "upcoming";
    statusControl.hidden = currentState !== "active";
    requestPagination.hidden = false;
    pageSummaryDesktop.textContent = tr("Strona {page} z {pages} · {count} pozycji", { page: pagination.page, pages: pagination.total_pages, count: pagination.total_items });
    pageSummaryMobile.textContent = `${pagination.page}/${pagination.total_pages} · ${pagination.total_items}`;
    requestPagination.querySelector('[data-page-direction="previous"]').disabled = pagination.page <= 1;
    requestPagination.querySelector('[data-page-direction="next"]').disabled = pagination.page >= pagination.total_pages;
    note.textContent = notes[currentState];
    list.innerHTML = groups.length ? groups.map(groupCardMarkup).join("") : `<div class="empty-state"><div>◌</div><h2>${tr("Ta lista jest pusta")}</h2><p>${tr(currentState === "active" ? "Dodaj pierwszy request, korzystając z wyszukiwarki TMDB." : "Nic jeszcze tutaj nie trafiło.")}</p></div>`;
    Object.keys(states).forEach((state) => {
      const count = document.querySelector(`[data-count="${state}"]`);
      if (count) count.textContent = paginationByState[state].total_all_items;
    });
    updateLikeCountdowns();
  }

  function requestSyncBlocked() {
    if (document.hidden || document.querySelector("dialog[open]")) return true;
    if (list.querySelector(".inline-confirm, .confirm-status:not([hidden]), details[open]")) return true;
    if (list.querySelector("button:disabled:not(.locked)")) return true;
    const focused = document.activeElement;
    return Boolean(focused && list.contains(focused) && focused.matches("input, select, textarea, button"));
  }

  async function loadAll({ silent = false } = {}) {
    if (silent && requestSyncBlocked()) return;
    while (requestLoadPromise) {
      if (silent) return requestLoadPromise;
      await requestLoadPromise;
    }
    if (!silent) list.innerHTML = `<div class="loading-state"><span></span><p>${tr("Wczytywanie listy…")}</p></div>`;
    const operation = (async () => {
      try {
        const results = await Promise.all(Object.keys(states).map((state) => {
          const query = new URLSearchParams({
            state,
            page: String(pageByState[state]),
            page_size: pageSizeSelect.value,
            sort: sortSelect.value,
          });
          if (state === "active") query.set("status_filter", statusFilter.value);
          return api(`/api/requests?${query}`);
        }));
        const normalizedResults = Object.keys(states).map((state, index) =>
          normalizeRequestResult(state, results[index])
        );
        const fingerprint = JSON.stringify(normalizedResults);
        const changed = fingerprint !== stateFingerprint;
        if (changed) {
          Object.keys(states).forEach((state, index) => {
            states[state] = normalizedResults[index].items;
            paginationByState[state] = normalizedResults[index].pagination;
            pageByState[state] = normalizedResults[index].pagination.page;
          });
          stateFingerprint = fingerprint;
        }
        if (changed || !silent) render();
      } catch (error) {
        console.error(tr("Nie udało się wczytać list requestów."), error);
        if (!silent) list.innerHTML = `<div class="empty-state error"><h2>${tr("Nie udało się wczytać listy")}</h2><p>${escapeHtml(error.message)}</p></div>`;
      }
    })();
    requestLoadPromise = operation;
    try {
      await operation;
    } finally {
      if (requestLoadPromise === operation) requestLoadPromise = null;
    }
  }

  document.querySelectorAll("[data-state]").forEach((tab) => tab.addEventListener("click", () => {
    currentState = tab.dataset.state;
    document.querySelectorAll("[data-state]").forEach((item) => item.classList.toggle("active", item === tab));
    render();
  }));
  statusFilter.addEventListener("change", async () => {
    pageByState.active = 1;
    await loadAll();
  });
  sortSelect.addEventListener("change", async () => {
    Object.keys(pageByState).forEach((state) => { pageByState[state] = 1; });
    await loadAll();
  });
  pageSizeSelect.addEventListener("change", async () => {
    Object.keys(pageByState).forEach((state) => { pageByState[state] = 1; });
    await loadAll();
  });
  requestPagination.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-page-direction]");
    if (!button) return;
    pageByState[currentState] += button.dataset.pageDirection === "next" ? 1 : -1;
    await loadAll();
  });

  async function copyText(value) {
    try { await navigator.clipboard.writeText(value); }
    catch (_) {
      const area = document.createElement("textarea"); area.value = value; area.style.position = "fixed"; area.style.opacity = "0";
      document.body.append(area); area.select(); document.execCommand("copy"); area.remove();
    }
    toast(tr("Skopiowano: {value}", { value }));
  }

  list.addEventListener("change", (event) => {
    const select = event.target.closest("[data-status-select]");
    if (!select) return;
    const confirm = select.closest(".status-admin-wrap").querySelector('[data-action="confirm-status"]');
    confirm.hidden = select.value === select.dataset.current;
  });

  list.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-action]");
    if (!button) return;
    const action = button.dataset.action;
    if (action === "copy") return copyText(button.dataset.copyValue);
    if (action === "switch-season") {
      const item = states[currentState].find((entry) => entry.id === Number(button.dataset.requestId));
      if (!item) return;
      activeSeasonByGroup.set(seasonGroupKey(item), item.id);
      render();
      return;
    }
    if (action === "delete") {
      deleteTarget = Number(button.dataset.id); deleteDialog.querySelector("[data-delete-title]").textContent = button.dataset.title;
      deleteForm.reset(); deleteDialog.showModal(); return;
    }
    if (action === "prepare-complete") {
      button.closest(".complete-control").innerHTML = `<span class="inline-confirm"><span>${tr("Na pewno?")}</span><button class="button success" type="button" data-action="confirm-complete" data-id="${button.dataset.id}">${tr("Potwierdź")}</button><button class="button" type="button" data-action="cancel-complete">${tr("Anuluj")}</button></span>`;
      return;
    }
    if (action === "withdraw") {
      const confirmed = window.confirm(
        tr("Wycofać ten request? Jeśli inni są zainteresowani, pozycja pozostanie bez Twojego udziału i dalszych powiadomień.")
      );
      if (!confirmed) return;
    }
    if (action === "cancel-complete") {
      const wrapper = button.closest(".complete-control");
      wrapper.innerHTML = `<button class="button success" type="button" data-action="prepare-complete" data-id="${wrapper.closest(".request-card").dataset.requestId}"><span class="complete-label-desktop">${tr("Oznacz jako zrealizowany")}</span><span class="complete-label-mobile">${tr("Wypełniony")}</span></button>`;
      return;
    }
    button.disabled = true;
    try {
      if (action === "like") {
        const result = await api(`/api/requests/${button.dataset.id}/like`, { method: "POST" });
        const item = Object.values(states).flat().find((entry) => entry.id === Number(button.dataset.id));
        if (item) { item.liked_by_me = result.liked; item.like_count = result.count; item.can_unlike = result.can_unlike; item.like_removal_deadline = result.like_removal_deadline; item.author_like = false; }
        await loadAll(); return;
      }
      if (action === "confirm-status") {
        const select = button.closest(".status-admin-wrap").querySelector("[data-status-select]");
        await api(`/api/requests/${button.dataset.id}/status`, { method: "PATCH", body: { status: select.value } });
        toast(tr("Status został zmieniony."));
      }
      if (action === "withdraw") {
        const result = await api(`/api/requests/${button.dataset.id}/withdraw`, { method: "POST" });
        toast(tr(result.result === "deleted" ? "Request został usunięty." : "Twój udział i dalsze powiadomienia zostały wycofane."));
      }
      if (action === "confirm-complete") {
        await api(`/api/requests/${button.dataset.id}/complete`, { method: "POST" });
        toast(tr("Pozycja trafiła do zrealizowanych."));
      }
      if (action === "restore") {
        await api(`/api/requests/${button.dataset.id}/restore`, { method: "POST" });
        toast(tr("Pozycja wróciła do requestów."));
      }
      await loadAll();
    } catch (error) { toast(error.message, "error"); button.disabled = false; }
  });

  deleteForm?.addEventListener("submit", async (event) => {
    event.preventDefault(); const button = deleteForm.querySelector('[type="submit"]'); button.disabled = true;
    try {
      await api(`/api/requests/${deleteTarget}`, { method: "DELETE", body: { reason: deleteForm.elements.reason.value.trim() } });
      deleteDialog.close(); toast(tr("Request został trwale usunięty.")); await loadAll();
    } catch (error) { toast(error.message, "error"); }
    finally { button.disabled = false; }
  });

  document.querySelector("[data-open-search]")?.addEventListener("click", () => {
    searchResults.innerHTML = `<div class="search-welcome"><strong>${tr("Wyszukiwarka TMDB")}</strong><p>${tr("Wpisz polski lub oryginalny tytuł filmu albo serialu.")}</p></div>`;
    searchForm.reset(); searchDialog.showModal(); setTimeout(() => searchForm.elements.query.focus(), 50);
  });

  function metadataLine(label, values) {
    return `<span><strong>${label}:</strong> ${values?.length ? values.map(escapeHtml).join(", ") : tr("brak danych")}</span>`;
  }

  function searchResultMarkup(item) {
    const poster = item.poster_url ? `<img src="${escapeHtml(item.poster_url)}" alt="" loading="lazy">` : `<div class="search-poster-empty">${tr("Brak\nokładki").replace("\n", "<br>")}</div>`;
    const label = item.media_type === "movie" ? tr("Film") : tr("Serial");
    const title = primaryTitle(item);
    const secondary = secondaryTitle(item);
    return `<article class="search-result search-result-v02" data-search-id="${item.tmdb_id}" data-media-type="${item.media_type}">
      ${poster}<div><span class="media-pill">${label}${item.year ? ` · ${item.year}` : ""}</span><h3>${escapeHtml(title)}</h3>
      ${secondary ? `<p class="search-original">${escapeHtml(secondary)}</p>` : ""}
      <div class="search-metadata">${metadataLine(tr("Kraj"), item.countries)}${metadataLine(item.media_type === "movie" ? tr("Reżyser") : tr("Twórcy/reżyser"), item.directors)}${metadataLine(tr("Obsada"), item.actors)}</div>
      <button class="button ${item.media_type === "movie" ? "primary" : ""}" type="button" data-search-action="${item.media_type === "movie" ? "add" : "seasons"}">${item.media_type === "movie" ? tr("Dodaj film") : tr("Wybierz sezony")}</button></div>
      <div class="season-picker" data-season-picker></div>
    </article>`;
  }

  searchForm.addEventListener("submit", async (event) => {
    event.preventDefault(); const submit = searchForm.querySelector('[type="submit"]'); submit.disabled = true;
    searchResults.innerHTML = `<div class="loading-state"><span></span><p>${tr("Pobieranie tytułów, twórców i obsady z TMDB…")}</p></div>`;
    try {
      const result = await api(`/api/tmdb/search?q=${encodeURIComponent(searchForm.elements.query.value.trim())}`);
      searchResults.innerHTML = result.items.length ? result.items.map(searchResultMarkup).join("") : `<div class="empty-small">${tr("Nie znaleziono pasujących filmów ani seriali.")}</div>`;
    } catch (error) { searchResults.innerHTML = `<div class="empty-small error">${escapeHtml(error.message)}</div>`; }
    finally { submit.disabled = false; }
  });

  async function addRequest(button, mediaType, tmdbId, seasonNumber = null) {
    button.disabled = true; const previous = button.textContent; button.textContent = tr("Dodawanie…");
    try {
      const result = await api("/api/requests", { method: "POST", body: { media_type: mediaType, tmdb_id: Number(tmdbId), season_number: seasonNumber } });
      searchDialog.close();
      await loadAll();
      let message = result.message;
      if (result.state === "upcoming") {
        message = result.duplicate
          ? tr("Ta pozycja znajduje się przed premierą. Dodano Twój like i pozostawiono ją w karcie „Przed premierą”.")
          : tr("Pozycja została sklasyfikowana jako przed premierą i umieszczona w karcie „Przed premierą”.");
      }
      if (result.warning) message += ` ${tr("Okładka nie została zapisana.")}`;
      toast(message, result.warning ? "warning" : result.state === "upcoming" ? "info" : "ok");
    } catch (error) { toast(error.message, "error"); button.disabled = false; button.textContent = previous; }
  }

  function seasonNoun(count) {
    if (language !== "pl") return tr(count === 1 ? "sezon" : "sezony");
    if (count === 1) return tr("sezon");
    const lastTwo = count % 100;
    if (lastTwo < 12 || lastTwo > 14) {
      const last = count % 10;
      if (last >= 2 && last <= 4) return tr("sezony");
    }
    return tr("sezonów");
  }

  function seasonPickerMarkup(seasons) {
    if (!seasons.length) return `<div class="empty-small">${tr("TMDB nie podało jeszcze żadnego sezonu.")}</div>`;
    return `<div class="season-picker-head">
      <div class="season-picker-title"><strong>${tr("Wybierz sezony")}</strong><span>${tr("Możesz dodać kilka sezonów w jednej operacji.")}</span></div>
      <button class="mini-button" type="button" data-search-action="toggle-seasons">${tr("Zaznacz wszystkie")}</button>
    </div>
    <div class="season-options">${seasons.map((season) => `<label class="season-option">
      <input type="checkbox" value="${season.season_number}" data-season-checkbox>
      <span class="season-option-copy"><strong>${tr("Sezon {season}", { season: season.season_number })}</strong><small>${season.air_date ? formatDate(season.air_date) : tr("data nieznana")}${season.episode_count ? ` · ${tr("{count} odc.", { count: season.episode_count })}` : ""}</small></span>
    </label>`).join("")}</div>
    <div class="season-picker-footer">
      <p class="season-batch-message" data-season-batch-message aria-live="polite"></p>
      <button class="button primary" type="button" data-search-action="add-seasons" disabled>${tr("Dodaj wybrane ({count})", { count: '<span data-season-selected-count>0</span>' })}</button>
    </div>`;
  }

  function updateSeasonSelection(picker) {
    const available = [...picker.querySelectorAll("[data-season-checkbox]:not(:disabled)")];
    const selected = available.filter((checkbox) => checkbox.checked);
    const addButton = picker.querySelector('[data-search-action="add-seasons"]');
    const toggleButton = picker.querySelector('[data-search-action="toggle-seasons"]');
    if (addButton) {
      addButton.disabled = selected.length === 0;
      const count = addButton.querySelector("[data-season-selected-count]");
      if (count) count.textContent = selected.length;
    }
    if (toggleButton) {
      toggleButton.disabled = available.length === 0;
      toggleButton.textContent = tr(available.length > 0 && selected.length === available.length ? "Odznacz wszystkie" : "Zaznacz wszystkie");
    }
  }

  function seasonBatchSummary(results) {
    const created = results.filter((item) => !item.result.duplicate).length;
    const joined = results.length - created;
    const upcoming = results.filter((item) => item.result.state === "upcoming").length;
    const warning = results.some((item) => item.result.warning);
    const parts = [];
    if (created) parts.push(tr("Dodano {count} {noun}", { count: created, noun: seasonNoun(created) }));
    if (joined) parts.push(tr("dołączono do {count} istniejących {noun}", { count: joined, noun: seasonNoun(joined) }));
    let message = `${parts.join(", ")}.`;
    if (upcoming) message += ` ${tr("{count} sezonów przed premierą umieszczono w karcie „Przed premierą”.", { count: upcoming })}`;
    if (warning) message += ` ${tr("Nie udało się zapisać co najmniej jednej okładki.")}`;
    return { message, upcoming, warning };
  }

  async function addSelectedSeasons(button, resultCard) {
    const picker = resultCard.querySelector("[data-season-picker]");
    const selected = [...picker.querySelectorAll("[data-season-checkbox]:checked:not(:disabled)")]
      .sort((left, right) => Number(left.value) - Number(right.value));
    if (!selected.length) return;

    const controls = [...picker.querySelectorAll("button, [data-season-checkbox]")];
    controls.forEach((control) => { control.disabled = true; });
    const messageBox = picker.querySelector("[data-season-batch-message]");
    messageBox.dataset.kind = "";
    messageBox.textContent = tr("Trwa dodawanie wybranych sezonów…");
    const successes = [];
    const failures = [];

    for (let index = 0; index < selected.length; index += 1) {
      const checkbox = selected[index];
      const season = Number(checkbox.value);
      button.textContent = tr("Dodawanie {current}/{total}…", { current: index + 1, total: selected.length });
      try {
        const result = await api("/api/requests", {
          method: "POST",
          body: { media_type: resultCard.dataset.mediaType, tmdb_id: Number(resultCard.dataset.searchId), season_number: season },
        });
        successes.push({ season, result, checkbox });
      } catch (error) {
        failures.push({ season, error, checkbox });
      }
    }

    if (successes.length) await loadAll();
    const successfulSeasons = new Set(successes.map((item) => item.season));
    [...picker.querySelectorAll("[data-season-checkbox]")].forEach((checkbox) => {
      const added = successfulSeasons.has(Number(checkbox.value));
      checkbox.disabled = added;
      checkbox.checked = !added && checkbox.checked;
      const label = checkbox.closest(".season-option");
      if (added) {
        label.classList.add("added");
        if (!label.querySelector(".season-added-mark")) {
          label.querySelector("strong").insertAdjacentHTML("beforeend", `<em class="season-added-mark"> · ${tr("dodano")}</em>`);
        }
      }
    });

    button.innerHTML = tr("Dodaj wybrane ({count})", { count: '<span data-season-selected-count>0</span>' });
    const toggleButton = picker.querySelector('[data-search-action="toggle-seasons"]');
    if (toggleButton) toggleButton.disabled = false;
    updateSeasonSelection(picker);

    if (!failures.length) {
      const summary = seasonBatchSummary(successes);
      searchDialog.close();
      toast(summary.message, summary.warning ? "warning" : summary.upcoming ? "info" : "ok");
      return;
    }

    const failureList = failures.map((item) => `${tr("sezon")} ${item.season}: ${escapeHtml(item.error.message)}`).join("; ");
    messageBox.dataset.kind = successes.length ? "warning" : "error";
    messageBox.innerHTML = `${successes.length ? `${tr("Dodano {count} {noun}", { count: successes.length, noun: seasonNoun(successes.length) })}. ` : ""}${tr("Nie udało się dodać: {failures}. Możesz spróbować ponownie.", { failures: failureList })}`;
    failures.forEach((item) => { item.checkbox.disabled = false; });
    updateSeasonSelection(picker);
    if (successes.length) {
      const summary = seasonBatchSummary(successes);
      toast(`${summary.message} ${tr("{count} {noun} wymaga ponowienia.", { count: failures.length, noun: seasonNoun(failures.length) })}`, "warning");
    } else {
      toast(tr("Nie udało się dodać wybranych sezonów."), "error");
    }
  }

  searchResults.addEventListener("change", (event) => {
    if (!event.target.matches("[data-season-checkbox]")) return;
    updateSeasonSelection(event.target.closest("[data-season-picker]"));
  });

  searchResults.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-search-action]"); if (!button) return;
    const resultCard = button.closest(".search-result"); const mediaType = resultCard.dataset.mediaType; const tmdbId = resultCard.dataset.searchId;
    if (button.dataset.searchAction === "add") return addRequest(button, mediaType, tmdbId);
    const picker = resultCard.querySelector("[data-season-picker]");
    if (button.dataset.searchAction === "toggle-seasons") {
      const available = [...picker.querySelectorAll("[data-season-checkbox]:not(:disabled)")];
      const shouldSelect = available.some((checkbox) => !checkbox.checked);
      available.forEach((checkbox) => { checkbox.checked = shouldSelect; });
      updateSeasonSelection(picker);
      return;
    }
    if (button.dataset.searchAction === "add-seasons") return addSelectedSeasons(button, resultCard);
    button.disabled = true; button.textContent = tr("Wczytywanie sezonów…");
    try {
      const details = await api(`/api/tmdb/${mediaType}/${tmdbId}`);
      picker.innerHTML = seasonPickerMarkup(details.seasons);
      button.remove();
    } catch (error) { toast(error.message, "error"); button.disabled = false; button.textContent = tr("Spróbuj ponownie"); }
  });

  const REQUEST_SYNC_INTERVAL_MS = 30000;
  function syncRequests() {
    if (!document.hidden) loadAll({ silent: true });
  }

  setInterval(updateLikeCountdowns, 250);
  window.setInterval(syncRequests, REQUEST_SYNC_INTERVAL_MS);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) syncRequests();
  });
  window.addEventListener("focus", syncRequests);
  loadAll();
})();
