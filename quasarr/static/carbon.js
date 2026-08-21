(function initializeTheme() {
	'use strict';
	let stored = '';
	try {
		stored = localStorage.getItem('quasarr_theme') || '';
	} catch (_error) {
		stored = '';
	}
	const validStored = stored === 'light' || stored === 'dark' ? stored : '';
	const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
	const theme = validStored || (prefersDark ? 'dark' : 'light');
	document.documentElement.setAttribute('data-carbon-theme', theme);
})();

(function bootstrapCarbonUi() {
	'use strict';

	const THEME_KEY = 'quasarr_theme';
	const VALID_THEMES = new Set(['light', 'dark']);
	let navElement = null;
	let navBackdrop = null;
	let navOpenButton = null;
	let navCloseButton = null;
	let navMediaQuery = null;
	let modalElement = null;
	let modalTitle = null;
	let modalEyebrow = null;
	let modalBody = null;
	let modalActions = null;
	let toastRegion = null;
	let shellElement = null;
	let lastFocusedElement = null;
	let navContentInertTargets = [];

	function currentTheme() {
		const theme = document.documentElement.getAttribute('data-carbon-theme');
		return VALID_THEMES.has(theme) ? theme : 'light';
	}

	function systemTheme() {
		return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
	}

	function storedThemePreference() {
		let stored = '';
		try {
			stored = localStorage.getItem(THEME_KEY) || '';
		} catch (_error) {
			stored = '';
		}
		return VALID_THEMES.has(stored) ? stored : 'system';
	}

	function setTheme(theme) {
		if (!VALID_THEMES.has(theme)) {
			return;
		}
		document.documentElement.setAttribute('data-carbon-theme', theme);
		try {
			localStorage.setItem(THEME_KEY, theme);
		} catch (_error) {
			// The selected theme still applies when storage is unavailable.
		}
		updateThemeControl();
		updateThemeSwitcher();
	}

	// Settings' Light | Dark | System switcher. "System" is not a third
	// stored theme: it clears the stored preference so the page falls back
	// to the OS media query, exactly like a first visit does.
	function applyThemePreference(preference) {
		if (preference === 'system') {
			try {
				localStorage.removeItem(THEME_KEY);
			} catch (_error) {
				// Nothing was stored, or storage is unavailable - either way
				// the media-query fallback below still applies.
			}
			document.documentElement.setAttribute('data-carbon-theme', systemTheme());
			updateThemeControl();
			updateThemeSwitcher();
			return;
		}
		setTheme(preference);
	}

	function updateThemeSwitcher() {
		const preference = storedThemePreference();
		document.querySelectorAll('[data-action="theme-switch"] input[name="theme"]').forEach(function (input) {
			input.checked = input.value === preference;
		});
	}

	function toggleTheme() {
		setTheme(currentTheme() === 'light' ? 'dark' : 'light');
	}

	function updateThemeControl() {
		const button = document.querySelector('[data-action="theme-toggle"]');
		if (!button) {
			return;
		}
		const nextTheme = currentTheme() === 'light' ? 'dark' : 'light';
		const label = `Switch to ${nextTheme} theme`;
		button.setAttribute('aria-label', label);
		button.setAttribute('title', label);
	}

	function getApiKey() {
		const meta = document.querySelector('meta[name="quasarr-api-key"]');
		return meta ? String(meta.getAttribute('content') || '') : '';
	}

	window.quasarrApiFetch = function quasarrApiFetch(url, options) {
		const requestOptions = Object.assign({}, options || {});
		let requestUrl;
		try {
			requestUrl = new URL(String(url), window.location.href);
		} catch (_error) {
			return Promise.reject(new TypeError('Invalid Quasarr API URL'));
		}
		if (requestUrl.origin !== window.location.origin) {
			return Promise.reject(new TypeError('Quasarr API requests must be same-origin'));
		}

		const headers = new Headers(requestOptions.headers || {});
		const apiKey = getApiKey();
		if (apiKey) {
			headers.set('X-Api-Key', apiKey);
		}
		requestOptions.headers = headers;
		return fetch(requestUrl.href, requestOptions);
	};

	function compactNavIsActive() {
		return Boolean(navMediaQuery && navMediaQuery.matches);
	}

	function updateNavAccessibility(isOpen) {
		if (!navElement) {
			return;
		}
		if (!compactNavIsActive()) {
			navElement.removeAttribute('aria-hidden');
			navElement.inert = false;
			return;
		}
		navElement.setAttribute('aria-hidden', String(!isOpen));
		navElement.inert = !isOpen;
	}

	function setNavContentInert(isOpen) {
		// The nav overlay must behave like a real modal for the content it
		// visually covers: main content (and the skip link, which would
		// otherwise offer a dead-end jump into now-inert content) go inert
		// - exactly like showModal()/closeModalInternal() do for the whole
		// shell - so a screen reader's virtual cursor can no longer reach
		// them while the overlay is open, not just the Tab key
		// (trapNavFocus already covers Tab). Scoped to leave the nav
		// ITSELF reachable (it lives inside .cds-shell too, so the modal's
		// own shellElement.inert toggle is not narrow enough to reuse
		// here) and the header untouched, since .cds-nav-backdrop's own
		// `inset: 48px 0 0 0` never visually covers it - it stays genuinely
		// visible and interactive while the overlay is open. `inert` is a
		// per-element attribute, so this composes safely with showModal()'s
		// shellElement.inert toggle in either open order: each element's
		// own explicit flag is unaffected by an ancestor's inert changing.
		navContentInertTargets.forEach(function (element) {
			if (!element) {
				return;
			}
			element.inert = isOpen;
			if (isOpen) {
				element.setAttribute('aria-hidden', 'true');
			} else {
				element.removeAttribute('aria-hidden');
			}
		});
	}

	function setNavOpen(isOpen, restoreFocus) {
		if (!navElement || !navBackdrop) {
			return;
		}
		const shouldOpen = Boolean(isOpen) && compactNavIsActive();
		if (!shouldOpen && restoreFocus && navOpenButton) {
			navOpenButton.focus();
		}
		navElement.classList.toggle('is-open', shouldOpen);
		navBackdrop.hidden = !shouldOpen;
		if (navOpenButton) {
			navOpenButton.setAttribute('aria-expanded', String(shouldOpen));
		}
		updateNavAccessibility(shouldOpen);
		setNavContentInert(shouldOpen);
		if (shouldOpen && navCloseButton) {
			navCloseButton.focus();
		}
	}

	function syncNavViewport() {
		if (!navElement || !navBackdrop) {
			return;
		}
		if (!compactNavIsActive()) {
			navElement.classList.remove('is-open');
			navBackdrop.hidden = true;
			if (navOpenButton) {
				navOpenButton.setAttribute('aria-expanded', 'false');
			}
			updateNavAccessibility(false);
			setNavContentInert(false);
			return;
		}
		setNavOpen(navElement.classList.contains('is-open'), false);
	}

	function copyFromTarget(targetId) {
		const source = document.getElementById(targetId);
		if (!source) {
			return;
		}
		const value = 'value' in source ? String(source.value) : String(source.textContent || '');
		if (!navigator.clipboard || typeof navigator.clipboard.writeText !== 'function') {
			window.showToast('Clipboard unavailable');
			return;
		}
		navigator.clipboard.writeText(value).then(
			function onCopied() {
				window.showToast('Copied to clipboard');
			},
			function onCopyFailed() {
				window.showToast('Copy failed');
			}
		);
	}

	function revealTarget(targetId) {
		const element = document.getElementById(targetId);
		if (!element || element.tagName !== 'INPUT') {
			return;
		}
		element.type = element.type === 'password' ? 'text' : 'password';
	}

	function getFocusable(root) {
		if (!root) {
			return [];
		}
		return Array.from(
			root.querySelectorAll(
				'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])'
			)
		);
	}

	function closeModalInternal() {
		if (!modalElement || modalElement.hidden) {
			return;
		}
		if (shellElement) {
			shellElement.inert = false;
			shellElement.removeAttribute('aria-hidden');
		}
		modalElement.hidden = true;
		modalBody.textContent = '';
		modalActions.textContent = '';
		if (lastFocusedElement && typeof lastFocusedElement.focus === 'function') {
			lastFocusedElement.focus();
		}
		lastFocusedElement = null;
	}

	window.closeModal = function closeModal() {
		closeModalInternal();
	};

	window.showModal = function showModal(title, bodyHtml, actionsHtml, options) {
		if (!modalElement) {
			return;
		}
		// Only capture the opener the FIRST time the modal opens from a
		// closed state. A chained call (e.g. a button rendered inside this
		// modal's own body opens a second, replacement dialog while
		// modalElement is still visible) must never overwrite this: at that
		// point document.activeElement is a node inside the modal body that
		// is about to be destroyed by the innerHTML replacement below, so
		// capturing it here would make closeModalInternal() try to focus a
		// detached element (a silent no-op) and focus would fall through to
		// document.body instead of returning to the page control that
		// started the chain.
		if (modalElement.hidden) {
			lastFocusedElement = document.activeElement;
		}
		// Design anatomy: an optional eyebrow line above the title, and an
		// optional wide surface for content-heavy dialogs (e.g. the mirrors
		// editor). Both are read from the same caller-owned `options` bag so
		// existing three-argument callers are unaffected.
		const eyebrowText = options && options.eyebrow ? String(options.eyebrow) : '';
		if (modalEyebrow) {
			modalEyebrow.textContent = eyebrowText;
			modalEyebrow.hidden = !eyebrowText;
		}
		const surface = modalElement.querySelector('.cds-modal__surface');
		if (surface) {
			surface.classList.toggle('cds-modal__surface--wide', Boolean(options && options.wide));
		}
		// options.titleMonoSuffix (e.g. " · nx.example.invalid") renders in
		// its own Mono-styled node appended after the plain-text title -
		// design spec §2.4. Every existing 3-argument caller, and every
		// 4-argument caller that doesn't pass this field, keeps getting a
		// plain-text title exactly as before.
		const titleMonoSuffix =
			options && options.titleMonoSuffix ? String(options.titleMonoSuffix) : '';
		modalTitle.textContent = '';
		modalTitle.appendChild(document.createTextNode(String(title || 'Dialog')));
		if (titleMonoSuffix) {
			const suffixEl = document.createElement('span');
			suffixEl.className = 'cds-mono';
			suffixEl.textContent = titleMonoSuffix;
			modalTitle.appendChild(suffixEl);
		}
		// Compatibility: caller-owned body/actions fragments are intentionally injected.
		modalBody.innerHTML = String(bodyHtml || '');
		modalActions.innerHTML = String(actionsHtml || '');
		modalElement.hidden = false;

		const focusable = getFocusable(modalElement);
		if (focusable.length > 0) {
			focusable[0].focus();
		} else {
			modalElement.focus();
		}
		if (shellElement) {
			shellElement.inert = true;
			shellElement.setAttribute('aria-hidden', 'true');
		}
	};

	function trapModalFocus(event) {
		if (!modalElement || modalElement.hidden || event.key !== 'Tab') {
			return;
		}
		const focusable = getFocusable(modalElement);
		if (focusable.length === 0) {
			event.preventDefault();
			return;
		}

		const first = focusable[0];
		const last = focusable[focusable.length - 1];
		if (event.shiftKey && document.activeElement === first) {
			event.preventDefault();
			last.focus();
			return;
		}
		if (!event.shiftKey && document.activeElement === last) {
			event.preventDefault();
			first.focus();
		}
	}

	function trapNavFocus(event) {
		// Below the 1056px breakpoint the side nav becomes a hamburger-
		// controlled overlay - it must trap Tab the same way the modal
		// does, or a keyboard user can Tab straight past its own last link
		// into the page content sitting behind the (visual-only) backdrop.
		if (
			!navElement ||
			event.key !== 'Tab' ||
			!compactNavIsActive() ||
			!navElement.classList.contains('is-open')
		) {
			return;
		}
		const focusable = getFocusable(navElement);
		if (focusable.length === 0) {
			event.preventDefault();
			return;
		}

		const first = focusable[0];
		const last = focusable[focusable.length - 1];
		if (event.shiftKey && document.activeElement === first) {
			event.preventDefault();
			last.focus();
			return;
		}
		if (!event.shiftKey && document.activeElement === last) {
			event.preventDefault();
			first.focus();
		}
	}

	function onClick(event) {
		const eventTarget = event.target instanceof Element ? event.target : null;
		const actionElement = eventTarget ? eventTarget.closest('[data-action]') : null;
		if (!actionElement) {
			if (modalElement && !modalElement.hidden && event.target === modalElement) {
				closeModalInternal();
			}
			return;
		}

		const action = actionElement.getAttribute('data-action');
		switch (action) {
			case 'nav-open':
				setNavOpen(true, false);
				break;
			case 'nav-close':
				setNavOpen(false, true);
				break;
			case 'theme-toggle':
				toggleTheme();
				break;
			case 'copy':
				copyFromTarget(actionElement.getAttribute('data-copy-target') || '');
				break;
			case 'reveal':
				revealTarget(actionElement.getAttribute('data-reveal-target') || '');
				break;
			case 'modal-close':
				closeModalInternal();
				break;
			default:
				break;
		}
	}

	function onChange(event) {
		const eventTarget = event.target instanceof Element ? event.target : null;
		if (!eventTarget) {
			return;
		}
		if (eventTarget.matches('.cds-toggle__input[role="switch"]')) {
			eventTarget.setAttribute('aria-checked', String(eventTarget.checked));
		}
		const actionElement = eventTarget.closest('[data-action]');
		if (!actionElement) {
			return;
		}
		const action = actionElement.getAttribute('data-action');
		if (action === 'theme-switch') {
			applyThemePreference(String(event.target.value || ''));
		}
	}

	function onKeydown(event) {
		if (event.key === 'Escape' && modalElement && !modalElement.hidden) {
			event.preventDefault();
			closeModalInternal();
			return;
		}
		if (event.key === 'Escape' && navElement && navElement.classList.contains('is-open')) {
			event.preventDefault();
			setNavOpen(false, true);
			return;
		}
		trapModalFocus(event);
		trapNavFocus(event);
	}

	window.showToast = function showToast(message) {
		if (!toastRegion) {
			return;
		}
		const toast = document.createElement('div');
		toast.className = 'cds-toast';
		toast.textContent = String(message || '');
		toastRegion.appendChild(toast);
		window.setTimeout(function removeToast() {
			toast.remove();
		}, 3000);
	};

	document.addEventListener('DOMContentLoaded', function onReady() {
		navElement = document.getElementById('cds-side-nav');
		navBackdrop = document.getElementById('cds-nav-backdrop');
		navOpenButton = document.querySelector('[data-action="nav-open"]');
		navCloseButton = document.querySelector('[data-action="nav-close"]');
		navMediaQuery = window.matchMedia('(max-width: 1056px)');
		modalElement = document.getElementById('cds-modal');
		modalTitle = document.getElementById('cds-modal-title');
		modalEyebrow = document.getElementById('cds-modal-eyebrow');
		modalBody = document.getElementById('cds-modal-body');
		modalActions = document.getElementById('cds-modal-actions');
		toastRegion = document.getElementById('cds-toast-region');
		shellElement = document.querySelector('.cds-shell');
		navContentInertTargets = [
			document.querySelector('.cds-skip-link'),
			document.getElementById('main-content')
		];

		updateThemeControl();
		updateThemeSwitcher();
		syncNavViewport();
		if (typeof navMediaQuery.addEventListener === 'function') {
			navMediaQuery.addEventListener('change', syncNavViewport);
		}

		document.addEventListener('click', onClick);
		document.addEventListener('change', onChange);
		document.addEventListener('keydown', onKeydown);
	});
})();

(function bootstrapCarbonDashboardAndSettings() {
	'use strict';

	function byId(id) {
		return document.getElementById(id);
	}

	function setFieldStatus(id, message) {
		var el = byId(id);
		if (!el) {
			return;
		}
		el.textContent = message || '';
	}

	function readFieldValue(id) {
		var el = byId(id);
		return el ? String(el.value || '').trim() : '';
	}

	function readCheckboxValue(id, fallback) {
		var el = byId(id);
		return el ? Boolean(el.checked) : Boolean(fallback);
	}

	async function fetchJsonSettings(url) {
		var response = await window.quasarrApiFetch(url);
		return response.json();
	}

	async function postJsonSettings(url, payload) {
		var response = await window.quasarrApiFetch(url, {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify(payload)
		});
		var data = await response.json();
		return { ok: response.ok, data: data };
	}

	function errorMessage(result, fallback) {
		return (result.data && result.data.message) || fallback;
	}

	// ---- Sequential saves. Two Settings tiles (Link protection, *arr
	// clients) now carry ONE Save that drives two independent existing
	// endpoints, and one Send test that drives every configured provider.
	// Every such helper resolves to { ok, message } instead of throwing or
	// writing its own status line, so a failure of the first call can
	// neither stop the second from running nor hide itself behind the
	// second one's result. combineResults() then names what failed AND
	// what still saved - a bare "Save failed" would leave the user unable
	// to tell which half of the tile actually applied. ----

	function combineResults(parts, successMessage, doneLabel) {
		var failed = parts.filter(function (part) {
			return !part.result.ok;
		});
		if (!failed.length) {
			return successMessage;
		}
		var message = failed
			.map(function (part) {
				return part.label + ': ' + part.result.message;
			})
			.join(' · ');
		var saved = parts
			.filter(function (part) {
				return part.result.ok;
			})
			.map(function (part) {
				return part.label;
			});
		if (saved.length) {
			message += ' · ' + saved.join(' and ') + ' ' + doneLabel + '.';
		}
		return message;
	}

	// ---- JDownloader (no GET endpoint exists for stored credentials, so
	// this always submits fresh user/pass/device input - same as Classic). ----

	async function verifyJDownloaderCredentials() {
		var statusId = 'settings-jd-status';
		setFieldStatus(statusId, 'Verifying credentials...');
		try {
			var result = await postJsonSettings('/api/jdownloader/verify', {
				user: readFieldValue('settings-jd-user'),
				pass: readFieldValue('settings-jd-pass')
			});
			if (!result.ok || !result.data.success) {
				throw new Error(errorMessage(result, 'Verification failed'));
			}
			var select = byId('settings-jd-device');
			if (select) {
				var currentDevice = select.getAttribute('data-current') || '';
				select.innerHTML = '';
				(result.data.devices || []).forEach(function (device) {
					var option = document.createElement('option');
					option.value = device;
					option.textContent = device;
					if (device === currentDevice) {
						option.selected = true;
					}
					select.appendChild(option);
				});
			}
			setFieldStatus(statusId, 'Credentials verified');
		} catch (error) {
			setFieldStatus(statusId, error.message);
		}
	}

	async function saveJDownloaderSettings() {
		var statusId = 'settings-jd-status';
		setFieldStatus(statusId, 'Saving...');
		try {
			var deviceSelect = byId('settings-jd-device');
			var result = await postJsonSettings('/api/jdownloader/save', {
				user: readFieldValue('settings-jd-user'),
				pass: readFieldValue('settings-jd-pass'),
				device: deviceSelect ? deviceSelect.value : ''
			});
			if (!result.ok || !result.data.success) {
				throw new Error(errorMessage(result, 'Save failed'));
			}
			setFieldStatus(statusId, result.data.message || 'Saved');
		} catch (error) {
			setFieldStatus(statusId, error.message);
		}
	}

	// ---- Radarr / Sonarr: fetch the authenticated settings before saving so
	// updating the URL alone can never blank an untouched, already-stored API
	// key. Clearing both fields is a separate explicit action. ----

	async function saveArrSettings(service) {
		try {
			var current = await fetchJsonSettings('/api/' + service + '/settings');
			var base = (current && current.settings) || {};
			var typedApiKey = readFieldValue('settings-' + service + '-api-key');
			var payload = {
				url: readFieldValue('settings-' + service + '-url'),
				api_key: typedApiKey || base.api_key || ''
			};
			var result = await postJsonSettings('/api/' + service + '/settings', payload);
			if (!result.ok || !result.data.success) {
				throw new Error(errorMessage(result, 'Save failed'));
			}
			return { ok: true, message: result.data.message || 'Saved' };
		} catch (error) {
			return { ok: false, message: error.message };
		}
	}

	function arrServiceLabel(service) {
		return service === 'sonarr' ? 'Sonarr' : 'Radarr';
	}

	// One Save for the tile: Radarr first, then Sonarr, both always
	// attempted, both outcomes reported in the one shared status line.
	async function saveAllArrSettings() {
		var statusId = 'settings-arr-status';
		setFieldStatus(statusId, 'Saving...');
		var radarr = await saveArrSettings('radarr');
		var sonarr = await saveArrSettings('sonarr');
		setFieldStatus(
			statusId,
			combineResults(
				[
					{ label: arrServiceLabel('radarr'), result: radarr },
					{ label: arrServiceLabel('sonarr'), result: sonarr }
				],
				'Saved',
				'saved'
			)
		);
	}

	// Radarr and Sonarr share one status line, so every message names the
	// service it is about - otherwise "Cleared" next to two Clear buttons
	// says nothing about which client was removed.
	async function clearArrSettings(service) {
		var statusId = 'settings-arr-status';
		var label = arrServiceLabel(service);
		setFieldStatus(statusId, 'Clearing ' + label + '...');
		try {
			var result = await postJsonSettings('/api/' + service + '/settings', {
				url: '',
				api_key: ''
			});
			if (!result.ok || !result.data.success) {
				throw new Error(errorMessage(result, 'Clear failed'));
			}
			var urlInput = byId('settings-' + service + '-url');
			var keyInput = byId('settings-' + service + '-api-key');
			if (urlInput) {
				urlInput.value = '';
			}
			if (keyInput) {
				keyInput.value = '';
			}
			setFieldStatus(statusId, result.data.message || label + ' cleared');
		} catch (error) {
			setFieldStatus(statusId, label + ': ' + error.message);
		}
	}

	// Clear wipes a configured client immediately once confirmed, so it
	// gets the same confirm-modal treatment as "Restart Quasarr" and
	// "Delete package" - a danger action never fires straight off a click.
	function openArrClearModal(service) {
		if (typeof window.showModal !== 'function') {
			return;
		}
		var label = arrServiceLabel(service);
		var body = document.createElement('div');
		body.appendChild(
			buildEl('p', '', 'This removes the configured URL and API key for ' + label + '. This cannot be undone.')
		);
		window.showModal(
			'Clear ' + label + ' configuration?',
			body.innerHTML,
			'<button class="cds-btn cds-btn--secondary" type="button" data-action="modal-close">Cancel</button>' +
				'<button class="cds-btn cds-btn--danger" type="button" data-action="' +
				service +
				'-clear-confirm">Clear</button>',
			{ eyebrow: '*arr clients' }
		);
	}

	// ---- Notifications ----

	function notificationCaseKeys(provider) {
		var el = byId('settings-notification-' + provider + '-cases');
		if (!el) {
			return [];
		}
		try {
			var parsed = JSON.parse(el.getAttribute('data-cases') || '[]');
			return Array.isArray(parsed) ? parsed : [];
		} catch (_error) {
			return [];
		}
	}

	function mergedCaseMap(baseMap, provider, idSuffix) {
		var merged = Object.assign({}, baseMap || {});
		notificationCaseKeys(provider).forEach(function (caseKey) {
			var id = 'settings-notif-' + provider + '-' + caseKey + idSuffix;
			merged[caseKey] = readCheckboxValue(id, merged[caseKey]);
		});
		return merged;
	}

	// The section has one unified Save (Classic's original single-save
	// semantics): every rendered credential field and every rendered
	// toggle/silent checkbox for BOTH providers is read from the DOM
	// unconditionally, so a typed edit anywhere in this section is never
	// silently discarded regardless of which button (Save, or a
	// provider's Send Test) triggered the save. The pre-save fetch is
	// still what protects a notification case this page's own rendered
	// case list does not know about (see mergedCaseMap) - never a source
	// of truth for a field this page *does* render.
	async function buildNotificationsPayload() {
		var current = await fetchJsonSettings('/api/notifications/settings');
		var base = (current && current.settings) || {};

		return {
			discord_webhook: readFieldValue('settings-notification-discord-webhook'),
			telegram_bot_token: readFieldValue('settings-notification-telegram-token'),
			telegram_chat_id: readFieldValue('settings-notification-telegram-chat-id'),
			toggles: {
				discord: mergedCaseMap((base.toggles || {}).discord, 'discord', ''),
				telegram: mergedCaseMap((base.toggles || {}).telegram, 'telegram', '')
			},
			silent: {
				discord: mergedCaseMap((base.silent || {}).discord, 'discord', '-silent'),
				telegram: mergedCaseMap((base.silent || {}).telegram, 'telegram', '-silent')
			}
		};
	}

	async function saveNotifications() {
		var statusId = 'settings-notifications-status';
		setFieldStatus(statusId, 'Saving...');
		try {
			var payload = await buildNotificationsPayload();
			var result = await postJsonSettings('/api/notifications/settings', payload);
			if (!result.ok || !result.data.success) {
				throw new Error(errorMessage(result, 'Save failed'));
			}
			setFieldStatus(statusId, result.data.message || 'Saved');
			return true;
		} catch (error) {
			setFieldStatus(statusId, error.message);
			return false;
		}
	}

	// Which providers a test can reach at all, read from the same rendered
	// fields the save reads - the tile has one Send test, so it must cover
	// every provider the user actually configured rather than pick one and
	// leave the other untestable.
	function configuredNotificationProviders() {
		var providers = [];
		if (readFieldValue('settings-notification-discord-webhook')) {
			providers.push({ id: 'discord', label: 'Discord' });
		}
		if (
			readFieldValue('settings-notification-telegram-token') &&
			readFieldValue('settings-notification-telegram-chat-id')
		) {
			providers.push({ id: 'telegram', label: 'Telegram' });
		}
		return providers;
	}

	async function testNotificationProvider(provider) {
		try {
			var result = await postJsonSettings('/api/notifications/test', { provider: provider });
			if (!result.ok || !result.data.success) {
				throw new Error(errorMessage(result, 'Failed to send test message'));
			}
			return { ok: true, message: result.data.message || 'Test message sent' };
		} catch (error) {
			return { ok: false, message: error.message };
		}
	}

	async function testConfiguredNotificationProviders() {
		var statusId = 'settings-notifications-status';
		var providers = configuredNotificationProviders();
		if (!providers.length) {
			setFieldStatus(statusId, 'Configure Discord or Telegram before sending a test.');
			return;
		}
		setFieldStatus(statusId, 'Saving settings before test...');
		var saved = await saveNotifications();
		if (!saved) {
			// saveNotifications() already wrote the real reason into this
			// same status line; a generic sentence here would throw away
			// the only clue the user has about what to fix.
			return;
		}
		setFieldStatus(statusId, 'Sending test message...');
		var results = [];
		for (var index = 0; index < providers.length; index += 1) {
			results.push({
				label: providers[index].label,
				result: await testNotificationProvider(providers[index].id)
			});
		}
		setFieldStatus(statusId, combineResults(results, 'Test message sent', 'sent'));
	}

	// ---- Timeouts ----

	function timeoutSlowModeKeys() {
		// Scoped to the toggle <input> itself, never a bare `[id^=...]`
		// attribute selector - toggle()'s own help text (unused here today,
		// but a real option the component supports) renders a sibling
		// <p id="settings-timeout-<key>-help">, which an unscoped selector
		// would also match and turn into a phantom "<key>-help" entry in
		// the POST payload.
		return Array.prototype.map.call(
			document.querySelectorAll('input[id^="settings-timeout-"]'),
			function (el) {
				return el.id.replace('settings-timeout-', '');
			}
		);
	}

	// The Save button is gone - a timeout switch saves as soon as it is
	// flipped - so the row's "Current: n s (normal|slow)" line has to
	// follow immediately. Both strings are rendered server-side onto the
	// row wrapper, so the multiplier stays owned by quasarr/constants and
	// is never recomputed here.
	function updateTimeoutHelpText(input) {
		var row = input.closest('[data-timeout-help-normal]');
		var help = byId(input.id + '-help');
		if (!row || !help) {
			return;
		}
		help.textContent = input.checked
			? row.getAttribute('data-timeout-help-slow') || ''
			: row.getAttribute('data-timeout-help-normal') || '';
	}

	// Re-sync every timeout switch and its help line from an authoritative
	// settings object, the same way saveFilecryptSetting() and
	// saveCrypterBlockSettings() re-sync their own controls from the
	// response. With no Save button in this tile the switch IS the state
	// indicator, so it must never be left showing a value the server does
	// not hold.
	function applyTimeoutSettings(settings) {
		timeoutSlowModeKeys().forEach(function (key) {
			var input = byId('settings-timeout-' + key);
			if (!input || typeof settings[key] === 'undefined') {
				return;
			}
			input.checked = !!settings[key];
			input.setAttribute('aria-checked', String(input.checked));
			updateTimeoutHelpText(input);
		});
	}

	async function saveTimeoutSettings(previousSettings) {
		var statusId = 'settings-timeouts-status';
		setFieldStatus(statusId, 'Saving...');
		var stored = null;
		try {
			var current = await fetchJsonSettings('/api/timeouts/settings');
			var base = (current && current.settings) || {};
			stored = base;
			var settings = Object.assign({}, base);
			timeoutSlowModeKeys().forEach(function (key) {
				settings[key] = readCheckboxValue('settings-timeout-' + key, settings[key]);
			});
			var result = await postJsonSettings('/api/timeouts/settings', { settings: settings });
			if (!result.ok || !result.data.success) {
				throw new Error(errorMessage(result, 'Save failed'));
			}
			applyTimeoutSettings(result.data.settings || settings);
			setFieldStatus(statusId, result.data.message || 'Saved');
		} catch (error) {
			// Nothing was stored: put the switch and its "Current: n s" line
			// back to what the server actually holds - the freshly fetched
			// settings when the GET got through, otherwise the state the
			// switch had before the user flipped it, which the change
			// handler captured for exactly this case.
			applyTimeoutSettings(stored || previousSettings || {});
			setFieldStatus(statusId, error.message);
		}
	}

	// ---- Filecrypt enabled / Link protection block policy (B1) ----

	async function saveFilecryptSetting() {
		try {
			var enabled = readCheckboxValue('settings-filecrypt-enabled', true);
			var result = await postJsonSettings('/api/filecrypt/settings', { enabled: enabled });
			if (!result.ok || !result.data.success) {
				throw new Error(errorMessage(result, 'Save failed'));
			}
			var checkbox = byId('settings-filecrypt-enabled');
			if (checkbox) {
				checkbox.checked = !!result.data.enabled;
				checkbox.setAttribute('aria-checked', String(checkbox.checked));
			}
			return { ok: true, message: result.data.message || 'Saved' };
		} catch (error) {
			return { ok: false, message: error.message };
		}
	}

	async function saveCrypterBlockSettings() {
		try {
			var current = await fetchJsonSettings('/api/crypter-block/settings');
			var base = (current && current.settings) || {};

			var modeInput = document.querySelector('input[name="settings-crypter-block-mode"]:checked');
			var mode = modeInput ? modeInput.value : base.mode;

			var cooldownHours = Number.parseInt(readFieldValue('settings-crypter-cooldown-hours'), 10);
			if (!Number.isFinite(cooldownHours)) {
				cooldownHours = base.cooldown_hours;
			}

			var defaultCheckbox = byId('settings-filecrypt-sweep-window-default');
			var sweepWindowInput = byId('settings-filecrypt-sweep-window');
			var sweepWindowMinutes = null;
			if (!defaultCheckbox || !defaultCheckbox.checked) {
				var parsedWindow = Number.parseInt(sweepWindowInput ? sweepWindowInput.value : '', 10);
				sweepWindowMinutes = Number.isFinite(parsedWindow)
					? parsedWindow
					: base.filecrypt_sweep_window_minutes;
			}

			var result = await postJsonSettings('/api/crypter-block/settings', {
				mode: mode,
				cooldown_hours: cooldownHours,
				filecrypt_sweep_window_minutes: sweepWindowMinutes
			});
			if (!result.ok || !result.data.success) {
				throw new Error(errorMessage(result, 'Save failed'));
			}

			var settings = result.data.settings || {};
			var savedModeInput = byId('settings-crypter-block-mode-' + settings.mode);
			if (savedModeInput) {
				savedModeInput.checked = true;
			}
			if (sweepWindowInput && typeof settings.filecrypt_sweep_window_minutes !== 'undefined') {
				sweepWindowInput.value = settings.filecrypt_sweep_window_minutes;
				sweepWindowInput.disabled = settings.filecrypt_sweep_window_override == null;
			}
			if (defaultCheckbox) {
				defaultCheckbox.checked = settings.filecrypt_sweep_window_override == null;
				defaultCheckbox.setAttribute('aria-checked', String(defaultCheckbox.checked));
			}
			var cooldownInput = byId('settings-crypter-cooldown-hours');
			if (cooldownInput && typeof settings.cooldown_hours !== 'undefined') {
				cooldownInput.value = settings.cooldown_hours;
			}
			return { ok: true, message: result.data.message || 'Saved' };
		} catch (error) {
			return { ok: false, message: error.message };
		}
	}

	// One Save for the Link protection tile: the Filecrypt switch and the
	// linkcrypter block policy keep their own separate existing endpoints
	// and payloads, posted one after the other. Neither result is dropped.
	async function saveLinkProtectionSettings() {
		var statusId = 'settings-link-protection-status';
		setFieldStatus(statusId, 'Saving...');
		var filecrypt = await saveFilecryptSetting();
		var block = await saveCrypterBlockSettings();
		setFieldStatus(
			statusId,
			combineResults(
				[
					{ label: 'Filecrypt decryption', result: filecrypt },
					{ label: 'Access blocks', result: block }
				],
				'Saved',
				'saved'
			)
		);
	}

	// ---- FlareSolverr: single field, no merge - a blank URL is an
	// intentional "clear and skip" action, matching Classic exactly. ----

	async function saveFlareSolverrSettings() {
		var statusId = 'settings-flaresolverr-status';
		setFieldStatus(statusId, 'Saving...');
		try {
			var result = await postJsonSettings('/api/flaresolverr', {
				url: readFieldValue('settings-flaresolverr-url')
			});
			if (!result.ok || !result.data.success) {
				throw new Error(errorMessage(result, 'Save failed'));
			}
			setFieldStatus(statusId, result.data.message || 'Saved');
		} catch (error) {
			setFieldStatus(statusId, error.message);
		}
	}

	// ---- API key ----

	function confirmRegenerateApiKey() {
		if (typeof window.showModal !== 'function') {
			return;
		}
		window.showModal(
			'Regenerate API key?',
			'Regenerating replaces the current API key. Every *arr client using the old key will need to be updated.',
			'<button class="cds-btn cds-btn--secondary" type="button" data-action="modal-close">Cancel</button>' +
				'<a class="cds-btn cds-btn--danger" href="/regenerate-api-key">Regenerate</a>',
			{ eyebrow: 'API access' }
		);
	}

	// ---- Dashboard: top-three downloads, loaded after first paint so the
	// page shell always completes even when JDownloader is down. ----

	var QUEUE_STATUS_LABELS = {
		waiting_captcha: 'Waiting for CAPTCHA',
		downloading: 'Downloading',
		extracting: 'Extracting',
		queued: 'Queued'
	};
	var QUEUE_BAR_TONES = {
		waiting_captcha: 'warning',
		downloading: 'interactive',
		extracting: 'success',
		queued: 'interactive'
	};

	// buildEl()/buildProgress() are scoped to this IIFE the same way
	// QUEUE_STATUS_LABELS above already is: carbon.js ships one script whose
	// per-page IIFEs share nothing but the handful of documented window
	// exports, so the Dashboard's preview stays independent of the Downloads
	// page module even though both draw the same kind of bar.
	function buildEl(tagName, className, text) {
		var el = document.createElement(tagName);
		if (className) {
			el.className = className;
		}
		if (text !== undefined) {
			el.textContent = text;
		}
		return el;
	}

	// Same accessibility contract as every other Carbon progress bar: an
	// announced role, value bounds, and a name identifying the package. The
	// preview's bar carries no value text of its own (the meta line beside
	// the name already states it), which is why it is a distinct function
	// from the Downloads table's labelled `buildProgress()`.
	function buildPreviewProgress(pct, tone, ariaLabel) {
		var value = Math.max(0, Math.min(100, Number(pct) || 0));
		var wrap = buildEl('div', 'cds-progress-cell');
		var bar = buildEl('div', 'cds-progress');
		bar.setAttribute('role', 'progressbar');
		bar.setAttribute('aria-valuemin', '0');
		bar.setAttribute('aria-valuemax', '100');
		bar.setAttribute('aria-valuenow', String(Math.round(value)));
		bar.setAttribute('aria-label', String(ariaLabel || 'Progress'));
		var fill = buildEl('div', 'cds-progress__fill cds-progress__fill--' + tone);
		fill.style.width = value + '%';
		bar.appendChild(fill);
		wrap.appendChild(bar);
		return wrap;
	}

	// A package that is actually moving reports its own numbers; anything
	// else (waiting for a CAPTCHA, extracting, queued) reports what it is
	// waiting on, because a percentage says nothing there.
	function queuePreviewMeta(row) {
		var statusLabel = QUEUE_STATUS_LABELS[row.status] || String(row.status || '');
		if (row.status !== 'downloading') {
			return statusLabel;
		}
		var percent = String(row.percentage || 0) + '%';
		if (row.eta_unknown || !row.eta) {
			return percent;
		}
		return percent + ' · ' + row.eta + ' left';
	}

	function renderQueueMessage(message) {
		var content = byId('dashboard-queue-content');
		if (!content) {
			return;
		}
		content.textContent = message;
	}

	function renderQueueRows(rows) {
		var content = byId('dashboard-queue-content');
		if (!content) {
			return;
		}
		if (!rows.length) {
			content.textContent = 'No active downloads.';
			return;
		}
		var list = buildEl('ul', 'cds-queue-preview');
		rows.slice(0, 3).forEach(function (row) {
			var name = String(row.name || 'Unknown');
			var item = buildEl('li', 'cds-queue-preview__row');
			item.appendChild(buildEl('span', 'cds-release', name));
			item.appendChild(buildEl('span', 'cds-queue-preview__meta', queuePreviewMeta(row)));
			item.appendChild(
				buildPreviewProgress(
					row.percentage,
					QUEUE_BAR_TONES[row.status] || 'interactive',
					'Download progress: ' + name
				)
			);
			list.appendChild(item);
		});
		content.textContent = '';
		content.appendChild(list);
	}

	function loadDashboardQueue() {
		var tile = byId('dashboard-queue-tile');
		if (!tile || typeof window.quasarrApiFetch !== 'function') {
			return;
		}

		var controller = typeof AbortController === 'function' ? new AbortController() : null;
		var timeoutId = controller
			? window.setTimeout(function () {
					controller.abort();
				}, 8000)
			: null;

		window
			.quasarrApiFetch('/api/packages/list', controller ? { signal: controller.signal } : {})
			.then(function (response) {
				if (timeoutId) {
					window.clearTimeout(timeoutId);
				}
				if (!response.ok) {
					throw new Error('Queue request failed');
				}
				return response.json();
			})
			.then(function (data) {
				tile.setAttribute('data-state', 'loaded');
				if (!data || data.connected === false) {
					renderQueueMessage('JDownloader is not connected.');
					return;
				}
				renderQueueRows(Array.isArray(data.queue) ? data.queue : []);
			})
			.catch(function () {
				if (timeoutId) {
					window.clearTimeout(timeoutId);
				}
				tile.setAttribute('data-state', 'error');
				renderQueueMessage('Queue is unavailable right now.');
			});
	}

	function onSettingsDashboardClick(event) {
		var target = event.target instanceof Element ? event.target.closest('[data-action]') : null;
		if (!target) {
			return;
		}
		switch (target.getAttribute('data-action')) {
			case 'jd-verify':
				verifyJDownloaderCredentials();
				break;
			case 'jd-save':
				saveJDownloaderSettings();
				break;
			case 'arr-save':
				saveAllArrSettings();
				break;
			case 'radarr-clear-open':
				openArrClearModal('radarr');
				break;
			case 'sonarr-clear-open':
				openArrClearModal('sonarr');
				break;
			case 'radarr-clear-confirm':
				window.closeModal();
				clearArrSettings('radarr');
				break;
			case 'sonarr-clear-confirm':
				window.closeModal();
				clearArrSettings('sonarr');
				break;
			case 'notifications-save':
				saveNotifications();
				break;
			case 'notifications-test':
				testConfiguredNotificationProviders();
				break;
			case 'link-protection-save':
				saveLinkProtectionSettings();
				break;
			case 'flaresolverr-save':
				saveFlareSolverrSettings();
				break;
			case 'regenerate-api-key':
				confirmRegenerateApiKey();
				break;
			default:
				break;
		}
	}

	function onSettingsDashboardChange(event) {
		var target = event.target;
		if (!(target instanceof Element)) {
			return;
		}
		if (target.id === 'settings-filecrypt-sweep-window-default') {
			var sweepInput = byId('settings-filecrypt-sweep-window');
			if (sweepInput) {
				sweepInput.disabled = target.checked;
			}
			return;
		}
		// Timeouts have no Save button any more: flipping a switch is the
		// save. Same function, same endpoint, same merged payload the
		// button used to send. The browser has already flipped the switch
		// by the time this runs, so the pre-flip state is handed to the
		// save as the fallback it restores when even the GET fails.
		if (target.matches('input[id^="settings-timeout-"]')) {
			var previousSettings = {};
			previousSettings[target.id.replace('settings-timeout-', '')] = !target.checked;
			updateTimeoutHelpText(target);
			saveTimeoutSettings(previousSettings);
		}
	}

	document.addEventListener('DOMContentLoaded', function () {
		loadDashboardQueue();
		document.addEventListener('click', onSettingsDashboardClick);
		document.addEventListener('change', onSettingsDashboardChange);
	});
})();

(function bootstrapCarbonHostnamesAndCategories() {
	'use strict';

	function byId(id) {
		return document.getElementById(id);
	}

	function escapeAttr(value) {
		return String(value)
			.replace(/&/g, '&amp;')
			.replace(/"/g, '&quot;')
			.replace(/</g, '&lt;')
			.replace(/>/g, '&gt;');
	}

	function buildEl(tagName, className, text) {
		var el = document.createElement(tagName);
		if (className) {
			el.className = className;
		}
		if (text !== undefined) {
			el.textContent = text;
		}
		return el;
	}

	function readApiKey() {
		var meta = document.querySelector('meta[name="quasarr-api-key"]');
		return meta ? String(meta.getAttribute('content') || '') : '';
	}

	function readJsonData(elementId, attr) {
		var el = byId(elementId);
		if (!el) {
			return null;
		}
		try {
			return JSON.parse(el.getAttribute(attr) || 'null');
		} catch (_error) {
			return null;
		}
	}

	// Shared across every failure path below (import, credential check,
	// category add/edit/delete, restart, ...) - the "generic error" modal.
	function showErrorModal(message) {
		if (typeof window.showModal !== 'function') {
			return;
		}
		var body = document.createElement('div');
		body.appendChild(buildEl('p', '', String(message || 'Something went wrong.')));
		window.showModal(
			'Error',
			body.innerHTML,
			'<button class="cds-btn cds-btn--secondary" type="button" data-action="modal-close">Close</button>',
			{ eyebrow: 'Hostnames' }
		);
	}

	// ---- Hostnames: Save. POST /api/hostnames is unchanged - it reads
	// request.forms (not JSON) and answers a full HTML reconnect/fail page,
	// exactly like Classic's own browser form submit. The modal here is a
	// confirmation step in front of that same real navigation, so the
	// endpoint and its payload shape stay byte-identical. ----

	function submitHostnamesForm() {
		var form = byId('hostnames-form');
		if (!form) {
			return;
		}
		// The visible import-URL field lives outside <form> (it also drives
		// the Import modal), so it must be synced into the hidden field on
		// every submit - not just after a successful import - exactly like
		// Classic's validateHostnames(); otherwise typing or clearing the
		// URL and pressing Save silently reverts the stored value.
		var urlField = byId('hostname-import-url');
		var hiddenUrl = byId('hostnames-url-hidden');
		if (urlField && hiddenUrl) {
			hiddenUrl.value = String(urlField.value || '').trim();
		}
		var apiKey = readApiKey();
		if (apiKey) {
			var input = form.querySelector('input[name="apikey"]');
			if (!input) {
				input = document.createElement('input');
				input.type = 'hidden';
				input.name = 'apikey';
				form.appendChild(input);
			}
			input.value = apiKey;
		}
		form.submit();
	}

	function openHostnamesSaveModal() {
		if (typeof window.showModal !== 'function') {
			return;
		}
		var body = document.createElement('div');
		body.appendChild(buildEl(
			'p',
			'',
			'Save hostname changes now? Clearing a field removes that hostname. ' +
				'Quasarr reconnects affected sources after saving.'
		));
		window.showModal(
			'Save hostnames?',
			body.innerHTML,
			'<button class="cds-btn cds-btn--secondary" type="button" data-action="modal-close">Cancel</button>' +
				'<button class="cds-btn cds-btn--primary" type="button" data-action="hostnames-save-confirm">Save</button>',
			{ eyebrow: 'Hostnames' }
		);
	}

	// ---- Hostnames: filter by site code or hostname value. Follows the
	// Downloads page's applySearchFilter() shape (a term read from a search
	// input, rows hidden by a simple case-insensitive substring match) -
	// here the input carries data-action="hostname-filter" instead of a
	// fixed id, and each row is matched against both its data-hostname-id
	// (the site code, e.g. "ga") and its hostname input's current value. ----

	function applyHostnameFilter() {
		var input = document.querySelector('[data-action="hostname-filter"]');
		var term = input ? input.value.trim().toLowerCase() : '';
		document.querySelectorAll('.cds-hostname-table__row[data-hostname-id]').forEach(function (row) {
			if (!term) {
				row.hidden = false;
				return;
			}
			var id = String(row.getAttribute('data-hostname-id') || '').toLowerCase();
			var hostnameInput = row.querySelector('.cds-hostname-table__input');
			var hostnameValue = hostnameInput ? String(hostnameInput.value || '').toLowerCase() : '';
			row.hidden = id.indexOf(term) === -1 && hostnameValue.indexOf(term) === -1;
		});
	}

	// ---- Hostnames: Import from URL ----

	function applyImportedHostnames(hostnames) {
		var count = 0;
		Object.keys(hostnames || {}).forEach(function (id) {
			var input = byId('hostname-' + id);
			if (input) {
				input.value = hostnames[id];
				count += 1;
			}
		});
		return count;
	}

	function openHostnameImportModal() {
		if (typeof window.showModal !== 'function') {
			return;
		}
		var urlField = byId('hostname-import-url');
		var url = urlField ? String(urlField.value || '').trim() : '';

		var body = document.createElement('div');
		body.appendChild(buildEl(
			'p',
			'',
			'Fetch hostname definitions from a URL and review them before saving.'
		));
		var status = buildEl('p', 'cds-field__help', url ? 'Source: ' + url : 'No URL entered.');
		status.id = 'hostnames-import-modal-status';
		body.appendChild(status);

		window.showModal(
			'Import hostnames',
			body.innerHTML,
			'<button class="cds-btn cds-btn--secondary" type="button" data-action="modal-close">Cancel</button>' +
				'<button class="cds-btn cds-btn--primary" type="button" data-action="hostnames-import-confirm">Import</button>',
			{ eyebrow: 'Hostnames' }
		);
	}

	function performHostnameImport() {
		var statusEl = byId('hostnames-import-modal-status');
		var urlField = byId('hostname-import-url');
		var url = urlField ? String(urlField.value || '').trim() : '';
		if (!url) {
			if (statusEl) {
				statusEl.textContent = 'Please enter a URL.';
			}
			return;
		}
		if (statusEl) {
			statusEl.textContent = 'Importing...';
		}

		window
			.quasarrApiFetch('/api/hostnames/import-url', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ url: url })
			})
			.then(function (response) {
				return response.json();
			})
			.then(function (data) {
				if (!data.success) {
					throw new Error(data.error || 'Import failed');
				}
				var count = applyImportedHostnames(data.hostnames);
				var hidden = byId('hostnames-url-hidden');
				if (hidden) {
					hidden.value = url;
				}
				if (statusEl) {
					var message = 'Imported ' + count + ' hostname(s)';
					var errorCount = data.errors ? Object.keys(data.errors).length : 0;
					if (errorCount > 0) {
						message += ' (' + errorCount + ' invalid)';
					}
					statusEl.textContent = message + '. Review the fields, then Save.';
				}
			})
			.catch(function (error) {
				if (statusEl) {
					statusEl.textContent = error.message;
				}
			});
	}

	// ---- Hostnames: status detail, credentials check, FlareSolverr gate,
	// and skip-login management. Row data (including the uncontrolled
	// `details` exception text) is fetched fresh from the existing
	// authenticated GET /api/hostnames endpoint only when a status modal is
	// opened, and is written into the DOM solely as textContent inside that
	// modal - never into a data attribute, never logged. Credential inputs
	// always start blank: no stored password (or username) ever reaches
	// this page's JSON or markup. ----

	var lastStatusRow = null;

	function fetchHostnameRows() {
		return window
			.quasarrApiFetch('/api/hostnames')
			.then(function (response) {
				return response.json();
			})
			.then(function (data) {
				return Array.isArray(data.hostnames) ? data.hostnames : [];
			});
	}

	function isFlaresolverrSkipped() {
		var el = byId('hostnames-flaresolverr-skipped');
		return Boolean(el && el.getAttribute('data-skipped') === 'true');
	}

	function formatTimestamp(value) {
		if (!value) {
			return '';
		}
		var date = new Date(value);
		if (isNaN(date.getTime())) {
			return String(value);
		}
		return date.toLocaleString();
	}

	function openFlareSolverrRequiredModal() {
		if (typeof window.showModal !== 'function') {
			return;
		}
		var body = document.createElement('div');
		body.appendChild(buildEl(
			'p',
			'',
			'This site requires flaresolverr-next, which was skipped during setup. ' +
				'Configure it in Settings before checking credentials.'
		));
		window.showModal(
			'flaresolverr-next required',
			body.innerHTML,
			'<button class="cds-btn cds-btn--secondary" type="button" data-action="modal-close">Skip source</button>' +
				'<a class="cds-btn cds-btn--primary" href="/settings">Open settings</a>',
			{ eyebrow: 'Hostnames' }
		);
	}

	function openSkipLoginModal(row) {
		if (typeof window.showModal !== 'function') {
			return;
		}
		var body = document.createElement('div');
		body.appendChild(buildEl(
			'p',
			'',
			'Login was skipped for ' + row.label + '. Require login again? ' +
				'You will need to provide credentials before this site works.'
		));
		window.showModal(
			'Require login again?',
			body.innerHTML,
			'<button class="cds-btn cds-btn--secondary" type="button" data-action="modal-close">Cancel</button>' +
				'<button class="cds-btn cds-btn--primary" type="button" data-action="hostname-skip-login-clear" ' +
				'data-hostname-id="' + escapeAttr(row.id) + '">Require login</button>',
			{ eyebrow: 'Hostnames' }
		);
	}

	function clearHostnameSkipLogin(id) {
		window
			.quasarrApiFetch('/api/skip-login/' + encodeURIComponent(id), { method: 'DELETE' })
			.then(function (response) {
				return response.json();
			})
			.then(function (data) {
				if (!data.success) {
					throw new Error(data.error || 'Failed to clear skip-login');
				}
				window.closeModal();
				window.location.reload();
			})
			.catch(function (error) {
				showErrorModal(error.message);
			});
	}

	function performHostnameCredentialsCheck(id) {
		var userField = byId('hostname-cred-user');
		var passField = byId('hostname-cred-pass');
		var statusEl = byId('hostname-cred-status');
		var user = userField ? userField.value : '';
		var pass = passField ? passField.value : '';

		if (statusEl) {
			statusEl.textContent = 'Checking...';
		}

		window
			.quasarrApiFetch('/api/hostnames/check-credentials/' + encodeURIComponent(id), {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ user: user, password: pass })
			})
			.then(function (response) {
				return response.json();
			})
			.then(function (data) {
				if (statusEl) {
					statusEl.textContent = data.message || (data.success ? 'Saved' : 'Failed');
				}
				if (data.success) {
					window.setTimeout(function () {
						window.closeModal();
						window.location.reload();
					}, 600);
				}
			})
			.catch(function (error) {
				if (statusEl) {
					statusEl.textContent = error.message;
				}
			});
	}

	// Mirrors the Python _STATUS_TONE dict in quasarr/api/config/carbon.py -
	// this JS-built status line has no access to that server-side constant,
	// so the tone mapping is duplicated here deliberately.
	var STATUS_TONE = {
		ok: 'success',
		error: 'error',
		login_failed: 'error',
		skipped: 'warning',
		unset: 'neutral'
	};

	function buildStatusLine(row) {
		var tone = STATUS_TONE[row.status] || 'neutral';
		var classes = 'cds-status cds-status--' + tone;
		if (tone === 'warning' || tone === 'error' || tone === 'neutral') {
			classes += ' cds-status--tinted';
		}
		var line = buildEl('p', classes);
		var dot = document.createElement('span');
		dot.className = 'cds-status__dot';
		dot.setAttribute('aria-hidden', 'true');
		line.appendChild(dot);
		line.appendChild(document.createTextNode(row.status_title));
		return line;
	}

	function buildCredentialsSection(row) {
		var wrap = buildEl('div', 'cds-hostname-credentials');
		wrap.appendChild(buildEl('h3', '', 'Credentials'));

		if (row.requires_flaresolverr && isFlaresolverrSkipped()) {
			wrap.appendChild(buildEl(
				'p',
				'cds-hostname-credentials__warning',
				'This site requires flaresolverr-next, which was skipped. Configure it in Settings first.'
			));
		}

		var userField = buildEl('div', 'cds-field');
		var userLabel = buildEl('label', 'cds-field__label', 'Login');
		userLabel.setAttribute('for', 'hostname-cred-user');
		var userInput = document.createElement('input');
		userInput.className = 'cds-field__input';
		userInput.id = 'hostname-cred-user';
		userInput.type = 'text';
		userInput.autocomplete = 'off';
		userField.appendChild(userLabel);
		userField.appendChild(userInput);

		var passField = buildEl('div', 'cds-field');
		var passLabel = buildEl('label', 'cds-field__label', 'Password');
		passLabel.setAttribute('for', 'hostname-cred-pass');
		var passInput = document.createElement('input');
		passInput.className = 'cds-field__input';
		passInput.id = 'hostname-cred-pass';
		passInput.type = 'password';
		passInput.autocomplete = 'off';
		passField.appendChild(passLabel);
		passField.appendChild(passInput);

		wrap.appendChild(userField);
		wrap.appendChild(passField);

		// The check/save action itself now lives in the modal footer
		// (Close + "Check & save session" - design spec §3), not inside
		// this section, so only the status feedback line stays here.
		var status = buildEl('p', 'cds-field__help', '');
		status.id = 'hostname-cred-status';
		wrap.appendChild(status);

		return wrap;
	}

	function openHostnameStatusModal(id) {
		fetchHostnameRows()
			.then(function (rows) {
				var row = rows.filter(function (candidate) {
					return candidate.id === id;
				})[0];
				if (!row) {
					showErrorModal('Could not load status for this hostname.');
					return;
				}
				lastStatusRow = row;

				var body = document.createElement('div');
				body.appendChild(buildStatusLine(row));
				// Quick link to the configured hostname itself - built at
				// runtime from `row.hostname` (never a literal source
				// hostname in this file). Lives in the body, not the
				// footer: design spec §3's footer is defined as exactly
				// Close + "Check & save session", but this link is real
				// function this page offered before and must keep
				// offering (no loss of function).
				if (row.hostname) {
					var openHref = row.hostname;
					if (!/^https?:\/\//i.test(openHref)) {
						openHref = 'https:' + '//' + openHref;
					}
					var openLink = document.createElement('a');
					openLink.className = 'cds-btn cds-btn--tertiary';
					openLink.href = openHref;
					openLink.target = '_blank';
					openLink.rel = 'noopener noreferrer';
					openLink.textContent = 'Open ' + String(row.id).toUpperCase();
					body.appendChild(openLink);
				}
				body.appendChild(buildEl('p', '', row.details || 'No additional details available.'));
				if (row.timestamp) {
					var suffix = row.operation ? ' (' + row.operation + ')' : '';
					body.appendChild(buildEl(
						'p',
						'cds-field__help',
						'Last checked: ' + formatTimestamp(row.timestamp) + suffix
					));
				}
				if (row.skip_login) {
					var skipBtn = document.createElement('button');
					skipBtn.type = 'button';
					skipBtn.className = 'cds-btn cds-btn--secondary';
					skipBtn.textContent = 'Require login again';
					skipBtn.setAttribute('data-action', 'hostname-skip-login-open');
					body.appendChild(skipBtn);
				}

				var showCredentialsCheck = false;
				if (row.supports_login) {
					if (row.requires_flaresolverr && isFlaresolverrSkipped()) {
						var fsBtn = document.createElement('button');
						fsBtn.type = 'button';
						fsBtn.className = 'cds-btn cds-btn--secondary';
						fsBtn.textContent = 'flaresolverr-next required';
						fsBtn.setAttribute('data-action', 'hostname-flaresolverr-required');
						body.appendChild(fsBtn);
					} else {
						body.appendChild(buildCredentialsSection(row));
						showCredentialsCheck = true;
					}
				}

				// design spec §3 Hostnames: footer is exactly Close
				// (secondary) + "Check & save session" (primary, only when
				// credentials are supported) - the "Open <ID>" link lives in
				// the body above instead (see its own comment).
				var actions = '<button class="cds-btn cds-btn--secondary" type="button" data-action="modal-close">Close</button>';
				if (showCredentialsCheck) {
					actions += '<button class="cds-btn cds-btn--primary" type="button" ' +
						'data-action="hostname-credentials-check" data-hostname-id="' + escapeAttr(id) + '">' +
						'Check &amp; save session</button>';
				}

				window.showModal(
					row.label,
					body.innerHTML,
					actions,
					{
						eyebrow: 'Hostname status',
						// design spec §2.4: the title's hostname suffix
						// renders in Mono - showModal writes the base title
						// via textContent (plain text, no styling), so this
						// options field is the minimum addition needed to
						// give the suffix its own styled node.
						titleMonoSuffix: row.hostname ? ' · ' + row.hostname : ''
					}
				);
			})
			.catch(function () {
				showErrorModal('Could not load status for this hostname.');
			});
	}

	// ---- Hostnames: restart ----

	function openRestartModal() {
		if (typeof window.showModal !== 'function') {
			return;
		}
		var body = document.createElement('div');
		body.appendChild(buildEl('p', '', 'Restart Quasarr now? Any unsaved changes will be lost.'));
		window.showModal(
			'Restart Quasarr?',
			body.innerHTML,
			'<button class="cds-btn cds-btn--secondary" type="button" data-action="modal-close">Cancel</button>' +
				'<button class="cds-btn cds-btn--danger" type="button" data-action="hostname-restart-confirm">Restart</button>',
			{ eyebrow: 'Hostnames' }
		);
	}

	function performRestart() {
		window
			.quasarrApiFetch('/api/restart', { method: 'POST' })
			.then(function (response) {
				return response.json();
			})
			.then(function (data) {
				if (!data.success) {
					throw new Error(data.message || 'Restart failed');
				}
				window.closeModal();
				window.showToast('Restarting Quasarr...');
			})
			.catch(function (error) {
				showErrorModal(error.message);
			});
	}

	// ---- Categories: download-category mirror priority reflow ----

	function readHosterData() {
		return {
			all: readJsonData('categories-hoster-data', 'data-all-hosters') || [],
			tier1: readJsonData('categories-hoster-data', 'data-tier1-hosters') || []
		};
	}

	function readSourceData() {
		return {
			hostnames: readJsonData('categories-source-data', 'data-hostnames') || [],
			supported: readJsonData('categories-source-data', 'data-supported') || {}
		};
	}

	// Mirrors quasarr/providers/carbon_icons.py's reviewed "arrow--up"/
	// "arrow--down" icons (same Carbon source + sha256, recorded there) -
	// duplicated here as FIXED, non-interpolated markup (this object never
	// carries package data; only buildMirrorMoveIcon()'s closed `direction`
	// lookup touches it) because render_icon() only runs server-side and
	// these reorder buttons are built entirely client-side. Parsed via a
	// detached wrapper's innerHTML rather than createElementNS() - see the
	// Downloads IIFE's ROW_ACTION_ICON_MARKUP for the identical rationale
	// (the SVG namespace URI createElementNS() needs is itself a
	// web-address-shaped string literal this file's no-remote-URL contract
	// forbids).
	var MIRROR_MOVE_ICON_MARKUP = {
		up:
			'<svg viewBox="0 0 32 32" fill="currentColor" aria-hidden="true" focusable="false" class="cds-icon cds-icon--sm">' +
			'<polygon points="16 4 6 14 7.41 15.41 15 7.83 15 28 17 28 17 7.83 24.59 15.41 26 14 16 4"/></svg>',
		down:
			'<svg viewBox="0 0 32 32" fill="currentColor" aria-hidden="true" focusable="false" class="cds-icon cds-icon--sm">' +
			'<polygon points="24.59 16.59 17 24.17 17 4 15 4 15 24.17 7.41 16.59 6 18 16 28 26 18 24.59 16.59"/></svg>'
	};

	function buildMirrorMoveIcon(direction) {
		var markup = MIRROR_MOVE_ICON_MARKUP[direction];
		if (!markup) {
			return null;
		}
		var wrapper = document.createElement('div');
		wrapper.innerHTML = markup;
		return wrapper.firstElementChild;
	}

	function buildMirrorRow(hoster, isChecked, isTier1) {
		// isTier1 gates the tier-1 .cds-tag rendered below - it no longer
		// needs its own row-level class token: that class had exactly one
		// visual consumer (the removed bare star), and left inert (no CSS
		// rule) once the star was replaced by the tag.
		var row = buildEl(
			'div',
			'cds-mirror-row' + (isChecked ? ' is-selected' : '')
		);
		row.setAttribute('data-hoster', hoster);

		var rank = buildEl('span', 'cds-mirror-row__rank', '');
		row.appendChild(rank);

		var label = buildEl('label', 'cds-mirror-row__label');
		label.setAttribute('for', 'mirror-check-' + hoster);
		var checkbox = document.createElement('input');
		checkbox.type = 'checkbox';
		checkbox.className = 'cds-mirror-row__checkbox';
		checkbox.id = 'mirror-check-' + hoster;
		checkbox.checked = Boolean(isChecked);
		// showModal() receives body.innerHTML - a STRING, not these live DOM
		// nodes - and the `checked` PROPERTY set above never serializes into
		// that string. Setting the ATTRIBUTE too is what survives the
		// innerHTML round-trip; without it every previously saved mirror
		// silently renders unchecked after Edit.
		if (checkbox.checked) {
			checkbox.setAttribute('checked', '');
		}
		label.appendChild(checkbox);
		// Row anatomy: rank, checkbox, hoster name (Mono), recommendation
		// tag - the name must render before the tag it sits beside.
		label.appendChild(buildEl('span', 'cds-mirror-row__name cds-mono', hoster));
		if (isTier1) {
			// Carbon markup has no emoji. Reuses the existing
			// `.cds-tag` component idiom (already styled/themed) instead of a
			// bare glyph in an unstyled class - its visible text is its whole
			// accessible name, so no separate aria-label is needed.
			label.appendChild(buildEl('span', 'cds-tag cds-tag--blue', 'Recommended'));
		}
		row.appendChild(label);

		// Reorder controls are built for EVERY row, checked or not - only
		// their visibility is a display concern. The row's `is-selected`
		// class (set above at construction, and kept live afterward by
		// onChange -> reflowMirrorRows on every checkbox toggle) drives
		// the CSS pair below, so an arrow group appears the moment a row
		// is ticked and disappears the moment it is unticked, with no
		// need to reopen the modal:
		//   .cds-mirror-row__move { visibility: hidden; }
		//   .cds-mirror-row.is-selected .cds-mirror-row__move { visibility: visible; }
		var moveGroup = buildEl('span', 'cds-mirror-row__move');
		var upBtn = document.createElement('button');
		upBtn.type = 'button';
		upBtn.className = 'cds-icon-button';
		upBtn.setAttribute('data-action', 'mirror-move-up');
		upBtn.setAttribute('aria-label', 'Move ' + hoster + ' up');
		upBtn.setAttribute('title', 'Move up');
		var upIcon = buildMirrorMoveIcon('up');
		if (upIcon) {
			upBtn.appendChild(upIcon);
		}
		var downBtn = document.createElement('button');
		downBtn.type = 'button';
		downBtn.className = 'cds-icon-button';
		downBtn.setAttribute('data-action', 'mirror-move-down');
		downBtn.setAttribute('aria-label', 'Move ' + hoster + ' down');
		downBtn.setAttribute('title', 'Move down');
		var downIcon = buildMirrorMoveIcon('down');
		if (downIcon) {
			downBtn.appendChild(downIcon);
		}
		moveGroup.appendChild(upBtn);
		moveGroup.appendChild(downBtn);
		row.appendChild(moveGroup);

		return row;
	}

	// Stable partition: enabled rows float to the top (keeping relative
	// order), disabled rows sink below - same rule as Classic's reflow.
	function reflowMirrorRows() {
		var container = byId('mirror-sortable');
		if (!container) {
			return;
		}
		var rows = Array.prototype.slice.call(container.children);
		function isOn(row) {
			var checkbox = row.querySelector('.cds-mirror-row__checkbox');
			return Boolean(checkbox && checkbox.checked);
		}
		var enabled = rows.filter(isOn);
		var disabled = rows.filter(function (row) {
			return !isOn(row);
		});
		enabled.concat(disabled).forEach(function (row) {
			container.appendChild(row);
		});
		rows.forEach(function (row) {
			row.classList.toggle('is-selected', isOn(row));
		});
		enabled.forEach(function (row, index) {
			row.querySelector('.cds-mirror-row__rank').textContent = String(index + 1);
		});
		disabled.forEach(function (row) {
			row.querySelector('.cds-mirror-row__rank').textContent = '';
		});
	}

	function moveMirrorRow(row, direction) {
		var sibling = direction < 0 ? row.previousElementSibling : row.nextElementSibling;
		if (!sibling) {
			return;
		}
		var siblingCheckbox = sibling.querySelector('.cds-mirror-row__checkbox');
		if (!siblingCheckbox || !siblingCheckbox.checked) {
			return;
		}
		if (direction < 0) {
			row.parentNode.insertBefore(row, sibling);
		} else {
			row.parentNode.insertBefore(sibling, row);
		}
		reflowMirrorRows();
	}

	function openDownloadCategoryEditModal(name, currentMirrors) {
		if (typeof window.showModal !== 'function') {
			return;
		}
		var hosters = readHosterData();
		// Enabled mirrors keep their SAVED priority order (currentMirrors),
		// never the fixed hosters.all order - merely opening Edit and
		// pressing Save must not silently re-persist the whitelist in
		// hosters.all order, which would change which mirror is
		// auto-decrypted. Matches Classic's
		// `currentMirrors.filter(m => ALL_HOSTERS.includes(m))`.
		var selected = currentMirrors.filter(function (hoster) {
			return hosters.all.indexOf(hoster) !== -1;
		});
		var unselected = hosters.all.filter(function (hoster) {
			return selected.indexOf(hoster) === -1;
		});
		var ordered = selected.concat(unselected);

		var body = document.createElement('div');
		// Design spec §3 Categories: a real warn-notification component in
		// the body, not a bare tinted paragraph - matches the pattern every
		// other destructive/cautionary modal on this page already uses
		// (e.g. the Downloads delete confirmation).
		var warning = buildEl('section', 'cds-notification cds-notification--warning');
		warning.setAttribute('role', 'alert');
		warning.appendChild(buildEl(
			'p',
			'cds-notification__message',
			'The mirror whitelist only affects downloads, not search. If you enable specific ' +
				'mirrors, a release must include one of them or its download fails.'
		));
		body.appendChild(warning);
		body.appendChild(buildEl(
			'p',
			'cds-field__help',
			'Select mirrors to enable them. Enabled mirrors move to the top in priority order; ' +
				'the topmost found mirror is auto-decrypted. Use the arrows to reorder.'
		));

		var list = buildEl('div', 'cds-mirror-sortable');
		list.id = 'mirror-sortable';
		ordered.forEach(function (hoster) {
			list.appendChild(buildMirrorRow(
				hoster,
				currentMirrors.indexOf(hoster) !== -1,
				hosters.tier1.indexOf(hoster) !== -1
			));
		});
		body.appendChild(list);

		window.showModal(
			'Edit mirrors · ' + name,
			body.innerHTML,
			'<button class="cds-btn cds-btn--secondary" type="button" data-action="modal-close">Cancel</button>' +
				'<button class="cds-btn cds-btn--primary" type="button" data-action="download-category-save" ' +
				'data-category="' + escapeAttr(name) + '">Save mirrors</button>',
			{ eyebrow: 'Download category', wide: true }
		);
		reflowMirrorRows();
	}

	function saveDownloadCategoryMirrors(name) {
		var container = byId('mirror-sortable');
		var selected = [];
		if (container) {
			Array.prototype.forEach.call(
				container.querySelectorAll('.cds-mirror-row__checkbox:checked'),
				function (checkbox) {
					var row = checkbox.closest('.cds-mirror-row');
					if (row) {
						selected.push(row.getAttribute('data-hoster'));
					}
				}
			);
		}
		window
			.quasarrApiFetch('/api/categories/' + encodeURIComponent(name) + '/mirrors', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ mirrors: selected })
			})
			.then(function (response) {
				return response.json();
			})
			.then(function (data) {
				if (!data.success) {
					throw new Error(data.message || 'Save failed');
				}
				window.closeModal();
				window.location.reload();
			})
			.catch(function (error) {
				showErrorModal(error.message);
			});
	}

	function addDownloadCategory() {
		var input = byId('download-category-new-name');
		var name = input ? String(input.value || '').trim() : '';
		if (!name) {
			return;
		}
		window
			.quasarrApiFetch('/api/categories', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ name: name })
			})
			.then(function (response) {
				return response.json();
			})
			.then(function (data) {
				if (!data.success) {
					throw new Error(data.message || 'Add failed');
				}
				window.location.reload();
			})
			.catch(function (error) {
				showErrorModal(error.message);
			});
	}

	function openDownloadCategoryDeleteModal(name) {
		if (typeof window.showModal !== 'function') {
			return;
		}
		var body = document.createElement('div');
		body.appendChild(buildEl('p', '', 'Delete category "' + name + '"? This cannot be undone.'));
		window.showModal(
			'Delete · ' + name,
			body.innerHTML,
			'<button class="cds-btn cds-btn--secondary" type="button" data-action="modal-close">Cancel</button>' +
				'<button class="cds-btn cds-btn--danger" type="button" ' +
				'data-action="download-category-delete-confirm" data-category="' + escapeAttr(name) + '">Delete category</button>',
			{ eyebrow: 'Confirm deletion' }
		);
	}

	function deleteDownloadCategory(name) {
		window
			.quasarrApiFetch('/api/categories/' + encodeURIComponent(name), { method: 'DELETE' })
			.then(function (response) {
				return response.json();
			})
			.then(function (data) {
				if (!data.success) {
					throw new Error(data.message || 'Delete failed');
				}
				window.closeModal();
				window.location.reload();
			})
			.catch(function (error) {
				showErrorModal(error.message);
			});
	}

	// ---- Categories: search-source selectable tags. Empty selection means
	// all hostnames - this page never sends a synthetic "select all" list. ----

	function buildSourcePill(source, isChecked) {
		var label = buildEl('label', 'cds-pill' + (isChecked ? ' is-selected' : ''));
		var checkbox = document.createElement('input');
		checkbox.type = 'checkbox';
		checkbox.className = 'cds-pill__checkbox';
		checkbox.value = source;
		checkbox.checked = Boolean(isChecked);
		// Same innerHTML-serialization gap as buildMirrorRow() above: the
		// `checked` property alone never reaches the string showModal()
		// receives, so the attribute must be set too.
		if (checkbox.checked) {
			checkbox.setAttribute('checked', '');
		}
		label.appendChild(checkbox);
		label.appendChild(buildEl('span', '', source.toUpperCase()));
		return label;
	}

	function openSearchCategoryEditModal(catId, name, currentSources, baseCategoryId) {
		if (typeof window.showModal !== 'function') {
			return;
		}
		var sourceData = readSourceData();
		var parsedBase = parseInt(baseCategoryId, 10);
		var categoryForFilter = isNaN(parsedBase) ? catId : parsedBase;

		var body = document.createElement('div');
		body.appendChild(buildEl(
			'p',
			'cds-hostname-credentials__warning',
			'This affects search results. If specific hostnames are set, only those are searched.'
		));
		var pills = buildEl('div', 'cds-pills');
		sourceData.hostnames.forEach(function (source) {
			var supported = sourceData.supported[source];
			if (supported && supported.indexOf(categoryForFilter) === -1) {
				return;
			}
			pills.appendChild(buildSourcePill(source, currentSources.indexOf(source) !== -1));
		});
		body.appendChild(pills);

		window.showModal(
			'Edit hostnames · ' + name,
			body.innerHTML,
			'<button class="cds-btn cds-btn--secondary" type="button" data-action="modal-close">Cancel</button>' +
				'<button class="cds-btn cds-btn--primary" type="button" data-action="search-category-save" ' +
				'data-cat-id="' + escapeAttr(String(catId)) + '">Save</button>',
			{ eyebrow: 'Search category' }
		);
	}

	function saveSearchCategorySources(catId) {
		var modalBody = byId('cds-modal-body');
		var selected = [];
		if (modalBody) {
			Array.prototype.forEach.call(
				modalBody.querySelectorAll('.cds-pill__checkbox:checked'),
				function (checkbox) {
					selected.push(checkbox.value);
				}
			);
		}
		window
			.quasarrApiFetch(
				'/api/categories_search/' + encodeURIComponent(catId) + '/search_sources',
				{
					method: 'POST',
					headers: { 'Content-Type': 'application/json' },
					body: JSON.stringify({ search_sources: selected })
				}
			)
			.then(function (response) {
				return response.json();
			})
			.then(function (data) {
				if (!data.success) {
					throw new Error(data.message || 'Save failed');
				}
				window.closeModal();
				window.location.reload();
			})
			.catch(function (error) {
				showErrorModal(error.message);
			});
	}

	function addSearchCategory() {
		var select = byId('search-category-new-base');
		var baseType = select ? select.value : '';
		if (!baseType) {
			return;
		}
		window
			.quasarrApiFetch('/api/categories_search', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ base_type: baseType })
			})
			.then(function (response) {
				return response.json();
			})
			.then(function (data) {
				if (!data.success) {
					throw new Error(data.message || 'Add failed');
				}
				window.location.reload();
			})
			.catch(function (error) {
				showErrorModal(error.message);
			});
	}

	function openSearchCategoryDeleteModal(catId, name) {
		if (typeof window.showModal !== 'function') {
			return;
		}
		var body = document.createElement('div');
		body.appendChild(buildEl(
			'p',
			'',
			'Delete search category "' + name + '"? This cannot be undone.'
		));
		window.showModal(
			'Delete · ' + name,
			body.innerHTML,
			'<button class="cds-btn cds-btn--secondary" type="button" data-action="modal-close">Cancel</button>' +
				'<button class="cds-btn cds-btn--danger" type="button" ' +
				'data-action="search-category-delete-confirm" data-cat-id="' + escapeAttr(String(catId)) + '">Delete search category</button>',
			{ eyebrow: 'Confirm deletion' }
		);
	}

	function deleteSearchCategory(catId) {
		window
			.quasarrApiFetch('/api/categories_search/' + encodeURIComponent(catId), { method: 'DELETE' })
			.then(function (response) {
				return response.json();
			})
			.then(function (data) {
				if (!data.success) {
					throw new Error(data.message || 'Delete failed');
				}
				window.closeModal();
				window.location.reload();
			})
			.catch(function (error) {
				showErrorModal(error.message);
			});
	}

	// ---- Delegated click/change handling ----

	function onClick(event) {
		var target = event.target instanceof Element ? event.target.closest('[data-action]') : null;
		if (!target) {
			return;
		}
		var action = target.getAttribute('data-action');
		switch (action) {
			case 'hostnames-save-confirm':
				submitHostnamesForm();
				break;
			case 'hostname-import':
				openHostnameImportModal();
				break;
			case 'hostnames-import-confirm':
				performHostnameImport();
				break;
			case 'hostname-reset': {
				var hostnamesForm = byId('hostnames-form');
				if (hostnamesForm) {
					hostnamesForm.reset();
					// reset() reverts the filter <input> (it lives inside
					// this form) but fires no 'input' event, and
					// applyHostnameFilter() only runs on 'input' - without
					// this, rows a stale filter had hidden stay hidden even
					// though the now-empty filter box looks cleared.
					applyHostnameFilter();
				}
				break;
			}
			case 'hostname-status':
				openHostnameStatusModal(target.getAttribute('data-hostname-id') || '');
				break;
			case 'hostname-credentials-check':
				performHostnameCredentialsCheck(target.getAttribute('data-hostname-id') || '');
				break;
			case 'hostname-flaresolverr-required':
				openFlareSolverrRequiredModal();
				break;
			case 'hostname-skip-login-open':
				if (lastStatusRow) {
					openSkipLoginModal(lastStatusRow);
				}
				break;
			case 'hostname-skip-login-clear':
				clearHostnameSkipLogin(target.getAttribute('data-hostname-id') || '');
				break;
			case 'hostnames-restart-open':
				openRestartModal();
				break;
			case 'hostname-restart-confirm':
				performRestart();
				break;
			case 'download-category-edit':
				openDownloadCategoryEditModal(
					target.getAttribute('data-category') || '',
					JSON.parse(target.getAttribute('data-mirrors') || '[]')
				);
				break;
			case 'download-category-save':
				saveDownloadCategoryMirrors(target.getAttribute('data-category') || '');
				break;
			case 'download-category-add':
				addDownloadCategory();
				break;
			case 'download-category-delete':
				openDownloadCategoryDeleteModal(target.getAttribute('data-category') || '');
				break;
			case 'download-category-delete-confirm':
				deleteDownloadCategory(target.getAttribute('data-category') || '');
				break;
			case 'mirror-move-up': {
				var upRow = target.closest('.cds-mirror-row');
				if (upRow) {
					moveMirrorRow(upRow, -1);
				}
				break;
			}
			case 'mirror-move-down': {
				var downRow = target.closest('.cds-mirror-row');
				if (downRow) {
					moveMirrorRow(downRow, 1);
				}
				break;
			}
			case 'search-category-edit':
				openSearchCategoryEditModal(
					target.getAttribute('data-cat-id') || '',
					target.getAttribute('data-name') || '',
					JSON.parse(target.getAttribute('data-search-sources') || '[]'),
					target.getAttribute('data-base-category') || ''
				);
				break;
			case 'search-category-save':
				saveSearchCategorySources(target.getAttribute('data-cat-id') || '');
				break;
			case 'search-category-add':
				addSearchCategory();
				break;
			case 'search-category-delete':
				openSearchCategoryDeleteModal(
					target.getAttribute('data-cat-id') || '',
					target.getAttribute('data-name') || ''
				);
				break;
			case 'search-category-delete-confirm':
				deleteSearchCategory(target.getAttribute('data-cat-id') || '');
				break;
			default:
				break;
		}
	}

	function onChange(event) {
		var target = event.target;
		if (!(target instanceof Element)) {
			return;
		}
		if (target.classList.contains('cds-mirror-row__checkbox')) {
			reflowMirrorRows();
		}
		if (target.classList.contains('cds-pill__checkbox')) {
			var pillLabel = target.closest('.cds-pill');
			if (pillLabel) {
				pillLabel.classList.toggle('is-selected', target.checked);
			}
		}
	}

	function onInput(event) {
		var target = event.target;
		if (target instanceof Element && target.getAttribute('data-action') === 'hostname-filter') {
			applyHostnameFilter();
		}
	}

	// The Save button is a real type="submit" (design spec §3), but
	// submitHostnamesForm() still has to run first to sync the visible
	// import-URL field into the hidden one and inject the API key (see its
	// own comments) - a bare native submit would skip both. Intercepting
	// the form's own 'submit' event and routing through the existing
	// confirm-then-submit flow keeps that behaviour: submitHostnamesForm()
	// finishes by calling form.submit() directly, which (unlike a second
	// click on a submit button) never re-fires this 'submit' event.
	//
	// carbon.js is one bundle served on both the main config page (this
	// IIFE's page) AND the setup wizard's own standalone Hostnames step,
	// which renders a SECOND, unrelated `<form id="hostnames-form">` with a
	// real, uninterrupted native submit (see storage/setup/carbon.py) - the
	// id collision is fine day-to-day since the two pages never coexist in
	// one document, but this listener is registered unconditionally on
	// every Carbon page, so it must not fire there. The gate is the modal
	// host's actual presence, not a URL/page check and not the setup
	// form's data-guard-submit attribute (an incidental marker that could
	// be added to this form too, or dropped from that one, for unrelated
	// reasons - silently flipping this gate without anyone noticing):
	// openHostnamesSaveModal() -> showModal() is a hard no-op without
	// #cds-modal (see showModal's own `if (!modalElement) return;` guard),
	// which render_carbon_simple_page (the setup wizard's shell) never
	// renders and render_carbon_html (this page's shell) always does - so
	// checking for the element directly ties the interception to the one
	// capability it structurally requires, and cannot silently invert.
	function onHostnamesFormSubmit(event) {
		var form = event.target;
		if (!(form instanceof HTMLFormElement) || form.id !== 'hostnames-form') {
			return;
		}
		if (!document.getElementById('cds-modal')) {
			return;
		}
		event.preventDefault();
		openHostnamesSaveModal();
	}

	document.addEventListener('DOMContentLoaded', function () {
		document.addEventListener('click', onClick);
		document.addEventListener('change', onChange);
		document.addEventListener('input', onInput);
		document.addEventListener('submit', onHostnamesFormSubmit);
	});
})();

(function bootstrapCarbonCaptcha() {
	'use strict';

	// Every protected provider is solved in the user's own browser by a
	// Tampermonkey userscript - Quasarr never solves a CAPTCHA server-side.
	// This IIFE only owns the page chrome around that flow: tutorial
	// timing, first-use storage, reset, package selection, and the manual
	// paste-and-submit fallback. Tutorial link text is never assembled
	// here - it is pre-rendered server-side into a hidden container (see
	// quasarr/api/captcha/carbon.py) and only read out by openProvider(),
	// so this file never embeds an absolute URL.

	function currentPackageId() {
		var page = document.querySelector('.cds-captcha-page');
		return page ? String(page.getAttribute('data-package-id') || '') : '';
	}

	function attemptsStorageKey(packageId) {
		return 'captcha_attempts_' + packageId;
	}

	function showFailedAttemptsWarning() {
		var warning = document.getElementById('failed-attempts-warning');
		if (warning) {
			warning.hidden = false;
		}
	}

	// Three compatibility functions, kept window-scoped: the failed-
	// attempts counter is addressed by package ID and may be read or
	// cleared by other pages in this flow (e.g. a future success/delete
	// response) exactly as Classic's inline equivalents were.
	window.incrementCaptchaAttempts = function incrementCaptchaAttempts() {
		var packageId = currentPackageId();
		if (!packageId) {
			return 0;
		}
		var key = attemptsStorageKey(packageId);
		var current = 0;
		try {
			current = parseInt(localStorage.getItem(key) || '0', 10) + 1;
			localStorage.setItem(key, String(current));
		} catch (_error) {
			return current;
		}
		if (current >= 2) {
			showFailedAttemptsWarning();
		}
		return current;
	};

	window.getCaptchaAttempts = function getCaptchaAttempts() {
		var packageId = currentPackageId();
		if (!packageId) {
			return 0;
		}
		try {
			return parseInt(localStorage.getItem(attemptsStorageKey(packageId)) || '0', 10);
		} catch (_error) {
			return 0;
		}
	};

	window.clearCaptchaAttempts = function clearCaptchaAttempts() {
		var packageId = currentPackageId();
		if (!packageId) {
			return;
		}
		try {
			localStorage.removeItem(attemptsStorageKey(packageId));
		} catch (_error) {
			// Nothing stored to clear.
		}
	};

	function tutorialSeen(storageKey) {
		try {
			return localStorage.getItem(storageKey) === 'true';
		} catch (_error) {
			return false;
		}
	}

	function markTutorialSeen(storageKey) {
		try {
			localStorage.setItem(storageKey, 'true');
		} catch (_error) {
			// The user still proceeds even when storage is unavailable.
		}
	}

	function resetTutorial(storageKey) {
		try {
			localStorage.removeItem(storageKey);
		} catch (_error) {
			// Nothing stored to remove.
		}
		if (typeof window.showModal === 'function') {
			window.showModal(
				'Tutorial Reset',
				'<p>Tutorial reset! Click the Open button to see it again.</p>',
				'<button class="cds-btn cds-btn--primary" type="button" data-action="captcha-reload">Reload</button>',
				{ eyebrow: 'CAPTCHA · tutorial' }
			);
		}
	}

	// Same consequence and wording as Downloads' confirmDeletePackage() (the
	// button here promises exactly the same thing: "Delete package & files")
	// but this page has no table row to read a package id from - it stays
	// gated on the clicked element's own data-href, so onCaptchaClick()'s
	// 'package-delete' case is kept separate from Downloads' 'closest("tr")'
	// version rather than merged into one shared helper.
	function confirmDeleteCaptchaPackage(deleteHref) {
		if (typeof window.showModal !== 'function' || !deleteHref) {
			return;
		}
		var body =
			'<section class="cds-notification cds-notification--warning" role="alert">' +
			'<p class="cds-notification__message"><strong>Files are deleted too.</strong> The package is ' +
			'removed from JDownloader together with downloaded files. This cannot be undone.</p>' +
			'</section>';
		var actions =
			'<button class="cds-btn cds-btn--secondary" type="button" data-action="modal-close">Cancel</button>' +
			'<button class="cds-btn cds-btn--danger" type="button" id="captcha-confirm-delete">Delete package and files</button>';

		window.showModal('Delete package and files?', body, actions, { eyebrow: 'Confirm deletion' });
		var confirmButton = document.getElementById('captcha-confirm-delete');
		if (confirmButton) {
			confirmButton.addEventListener('click', function onConfirm() {
				confirmButton.removeEventListener('click', onConfirm);
				window.location.href = deleteHref;
			});
		}
	}

	function openProvider(url, storageKey) {
		if (tutorialSeen(storageKey)) {
			window.incrementCaptchaAttempts();
			window.location.href = url;
			return;
		}

		if (typeof window.showModal !== 'function') {
			return;
		}

		var source = document.getElementById('captcha-tutorial-content');
		var content = source ? source.innerHTML : '';
		var btnId = 'captcha-modal-proceed-' + Math.floor(Math.random() * 10000);
		var buttons =
			'<button class="cds-btn cds-btn--secondary" type="button" data-action="modal-close">Later</button>' +
			'<button id="' + btnId + '" class="cds-btn cds-btn--primary" type="button" disabled>Wait 5s...</button>';

		window.showModal('First Time Setup', content, buttons, { eyebrow: 'CAPTCHA · first time' });

		var count = 5;
		var btn = document.getElementById(btnId);
		var interval = window.setInterval(function tick() {
			if (!btn) {
				window.clearInterval(interval);
				return;
			}
			count -= 1;
			if (count <= 0) {
				window.clearInterval(interval);
				btn.textContent = 'I have installed Tampermonkey and the userscript';
				btn.disabled = false;
				btn.addEventListener('click', function onProceed() {
					markTutorialSeen(storageKey);
					if (typeof window.closeModal === 'function') {
						window.closeModal();
					}
					window.incrementCaptchaAttempts();
					window.location.href = url;
				});
			} else {
				btn.textContent = 'Wait ' + count + 's...';
			}
		}, 1000);
	}

	function onCaptchaClick(event) {
		var eventTarget = event.target instanceof Element ? event.target : null;
		var actionElement = eventTarget ? eventTarget.closest('[data-action]') : null;
		if (!actionElement) {
			return;
		}

		switch (actionElement.getAttribute('data-action')) {
			case 'captcha-open':
				openProvider(
					actionElement.getAttribute('data-open-url') || '',
					actionElement.getAttribute('data-storage-key') || ''
				);
				break;
			case 'captcha-reset-tutorial':
				resetTutorial(actionElement.getAttribute('data-storage-key') || '');
				break;
			case 'captcha-open-source': {
				var sourceUrl = actionElement.getAttribute('data-source-url') || '';
				if (sourceUrl) {
					window.open(sourceUrl, '_blank', 'noopener,noreferrer');
				}
				break;
			}
			case 'captcha-reload':
				window.location.reload();
				break;
			case 'package-delete': {
				var deleteHref = actionElement.getAttribute('data-href') || '';
				if (deleteHref) {
					confirmDeleteCaptchaPackage(deleteHref);
				}
				break;
			}
			default:
				break;
		}
	}

	function onCaptchaChange(event) {
		var eventTarget = event.target instanceof Element ? event.target : null;
		if (!eventTarget || eventTarget.getAttribute('data-action') !== 'captcha-package-select') {
			return;
		}
		var parts = String(eventTarget.value || '').split('|');
		if (parts.length !== 2) {
			return;
		}
		window.location.href = '/captcha/' + parts[0] + '?data=' + parts[1];
	}

	function onCaptchaSubmit(event) {
		var eventTarget = event.target instanceof Element ? event.target : null;
		if (!eventTarget || eventTarget.getAttribute('data-action') !== 'captcha-manual-submit') {
			return;
		}
		// Never blocks the native form submission - mirrors Classic's
		// onsubmit, which only ever counted the attempt.
		window.incrementCaptchaAttempts();
	}

	function onCaptchaToggle(event) {
		var details = event.target;
		if (!details || !details.hasAttribute || !details.hasAttribute('data-manual-submit')) {
			return;
		}
		var summary = details.querySelector('summary');
		if (!summary) {
			return;
		}
		summary.textContent = details.open ? 'Hide Manual Submission' : 'Show Manual Submission';
	}

	document.addEventListener('DOMContentLoaded', function onReady() {
		if (!document.querySelector('.cds-captcha-page')) {
			return;
		}

		document.querySelectorAll('[data-action="captcha-reset-tutorial"]').forEach(function (button) {
			if (tutorialSeen(button.getAttribute('data-storage-key') || '')) {
				button.hidden = false;
			}
		});

		if (window.getCaptchaAttempts() >= 2) {
			showFailedAttemptsWarning();
		}

		document.addEventListener('click', onCaptchaClick);
		document.addEventListener('change', onCaptchaChange);
		document.addEventListener('submit', onCaptchaSubmit);
		// 'toggle' does not bubble in every browser - capture phase still
		// reaches document regardless, so delegation stays reliable.
		document.addEventListener('toggle', onCaptchaToggle, true);
	});
})();

// ---- Standalone auth/system pages: success countdown-then-continue and
// reconnect countdown-then-poll. Both are page-load side effects (not
// clicks), so they scan for their marker elements at DOMContentLoaded
// instead of using the delegated data-action click dispatch above. Timings
// intentionally match the previous inline Classic scripts exactly: 1000ms
// countdown ticks, a HEAD request to '/' with no-store caching while
// reconnecting, and a 500ms pause before reloading once the server answers.
(function bootstrapCarbonStatusPages() {
	'use strict';

	function startCountdownRedirect(button) {
		var seconds = parseInt(button.getAttribute('data-seconds'), 10);
		if (!Number.isFinite(seconds) || seconds < 0) {
			seconds = 0;
		}
		var target = button.getAttribute('data-target') || '/';
		var remaining = seconds;

		function render() {
			button.textContent = remaining > 0 ? 'Wait time... ' + remaining : 'Continue';
		}

		function finish() {
			button.disabled = false;
			button.classList.remove('cds-btn--secondary');
			button.classList.add('cds-btn--primary');
			button.addEventListener('click', function onContinueClick() {
				window.location.href = target;
			});
		}

		render();

		if (remaining <= 0) {
			finish();
			return;
		}

		var interval = window.setInterval(function tick() {
			remaining--;
			render();
			if (remaining <= 0) {
				window.clearInterval(interval);
				finish();
			}
		}, 1000);
	}

	function startReconnectPoll(button) {
		var seconds = parseInt(button.getAttribute('data-seconds'), 10);
		if (!Number.isFinite(seconds) || seconds < 0) {
			seconds = 0;
		}
		var remaining = seconds;

		function render(text) {
			button.textContent = text;
		}

		function attemptReconnect() {
			var attempts = 0;

			function attempt() {
				attempts++;
				fetch('/', { method: 'HEAD', cache: 'no-store' })
					.then(function onHeadResponse(response) {
						if (response.ok) {
							render('Connected! Reloading...');
							button.classList.remove('cds-btn--secondary');
							button.classList.add('cds-btn--primary');
							window.setTimeout(function onReload() {
								window.location.href = '/';
							}, 500);
						} else {
							scheduleRetry();
						}
					})
					.catch(scheduleRetry);
			}

			function scheduleRetry() {
				render('Reconnecting... (attempt ' + attempts + ')');
				window.setTimeout(attempt, 1000);
			}

			attempt();
		}

		render(remaining > 0 ? 'Continuing in ' + remaining + '...' : 'Reconnecting...');

		if (remaining <= 0) {
			attemptReconnect();
			return;
		}

		var interval = window.setInterval(function tick() {
			remaining--;
			if (remaining <= 0) {
				window.clearInterval(interval);
				render('Reconnecting...');
				attemptReconnect();
			} else {
				render('Continuing in ' + remaining + '...');
			}
		}, 1000);
	}

	document.addEventListener('DOMContentLoaded', function onReady() {
		document
			.querySelectorAll('[data-action="continue-countdown"]')
			.forEach(startCountdownRedirect);
		document
			.querySelectorAll('[data-action="reconnect-poll"]')
			.forEach(startReconnectPoll);
		document
			.querySelectorAll('[data-action="jd-retry"]')
			.forEach(function (button) {
				button.addEventListener('click', function onRetryClick() {
					window.location.reload();
				});
			});
	});
})();

(function bootstrapCarbonSetupFlows() {
	'use strict';

	function byId(id) {
		return document.getElementById(id);
	}

	function setStatusText(id, message) {
		var el = byId(id);
		if (el) {
			el.textContent = String(message || '');
		}
	}

	// ---- Hostnames setup: import from URL. Same endpoint/response shape as
	// Classic's importHostnames() and the main Carbon Hostnames page - only
	// the DOM wiring differs (no modal, plain status text). ----

	function applyImportedHostnames(hostnames) {
		var count = 0;
		Object.keys(hostnames || {}).forEach(function (id) {
			var input = byId('hostname-' + id);
			if (input) {
				input.value = hostnames[id];
				count += 1;
			}
		});
		return count;
	}

	function performSetupHostnamesImport() {
		var urlField = byId('hostnames-import-url');
		var url = urlField ? String(urlField.value || '').trim() : '';
		if (!url) {
			setStatusText('setup-hostnames-import-status', 'Please enter a URL.');
			return;
		}
		setStatusText('setup-hostnames-import-status', 'Importing...');

		window
			.quasarrApiFetch('/api/hostnames/import-url', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ url: url })
			})
			.then(function (response) {
				return response.json();
			})
			.then(function (data) {
				if (!data.success) {
					throw new Error(data.error || 'Import failed');
				}
				var count = applyImportedHostnames(data.hostnames);
				var message = 'Imported ' + count + ' hostname(s)';
				var errorCount = data.errors ? Object.keys(data.errors).length : 0;
				if (errorCount > 0) {
					message += ' (' + errorCount + ' invalid)';
				}
				setStatusText(
					'setup-hostnames-import-status',
					message + '. Review the fields, then Save.'
				);
			})
			.catch(function (error) {
				setStatusText('setup-hostnames-import-status', error.message);
			});
	}

	// ---- Hostnames setup: per-row Details panel. The uncontrolled `details`
	// exception text (may embed a configured hostname) is fetched fresh from
	// the existing authenticated GET /api/hostnames endpoint only when a row
	// is expanded, and lands solely as textContent - never a data attribute,
	// never logged. Mirrors the privacy rule api/config/carbon.py documents
	// for the main Hostnames page's status modal. ----

	function fetchHostnameRows() {
		return window
			.quasarrApiFetch('/api/hostnames')
			.then(function (response) {
				return response.json();
			})
			.then(function (data) {
				return Array.isArray(data.hostnames) ? data.hostnames : [];
			});
	}

	function formatTimestamp(value) {
		if (!value) {
			return '';
		}
		var date = new Date(value);
		if (isNaN(date.getTime())) {
			return String(value);
		}
		return date.toLocaleString();
	}

	function toggleSetupHostnameDetails(button, id) {
		var panel = byId('hostname-details-' + id);
		if (!panel) {
			return;
		}
		var expand = panel.hidden;
		panel.hidden = !expand;
		button.setAttribute('aria-expanded', String(expand));
		if (!expand) {
			return;
		}

		var textEl = byId('hostname-details-text-' + id);
		if (textEl) {
			textEl.textContent = 'Loading...';
		}
		fetchHostnameRows()
			.then(function (rows) {
				var row = rows.filter(function (candidate) {
					return candidate.id === id;
				})[0];
				if (!textEl) {
					return;
				}
				if (!row) {
					textEl.textContent = 'Could not load status for this hostname.';
					return;
				}
				var text = row.details || 'No additional details available.';
				if (row.timestamp) {
					var suffix = row.operation ? ' in ' + row.operation : '';
					text += ' (Occurred' + suffix + ' at ' + formatTimestamp(row.timestamp) + ')';
				}
				textEl.textContent = text;
			})
			.catch(function () {
				if (textEl) {
					textEl.textContent = 'Could not load status for this hostname.';
				}
			});
	}

	// ---- Hostnames setup: credential check. Fields always start blank -
	// no stored password (or username) ever reaches this page's markup. ----

	function checkSetupHostnameCredentials(id) {
		var userField = byId('hostname-cred-user-' + id);
		var passField = byId('hostname-cred-pass-' + id);
		var statusId = 'hostname-cred-status-' + id;
		var user = userField ? userField.value : '';
		var password = passField ? passField.value : '';

		setStatusText(statusId, 'Checking...');

		window
			.quasarrApiFetch('/api/hostnames/check-credentials/' + id, {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ user: user, password: password })
			})
			.then(function (response) {
				return response.json();
			})
			.then(function (data) {
				setStatusText(statusId, data.message || (data.success ? 'Saved' : 'Failed'));
				if (!data.success) {
					return;
				}
				var tag = byId('hostname-status-tag-' + id);
				if (tag) {
					// Matches the row builder's plain (non-button) status
					// component (quasarr.providers.carbon_templates.status())
					// - a colored dot plus text, not a .cds-tag pill, which
					// the dense-row redesign replaced everywhere else.
					tag.innerHTML = '';
					var statusEl = document.createElement('span');
					statusEl.className = 'cds-status cds-status--success';
					var dot = document.createElement('span');
					dot.className = 'cds-status__dot';
					dot.setAttribute('aria-hidden', 'true');
					statusEl.appendChild(dot);
					statusEl.appendChild(document.createTextNode('Working normally'));
					tag.appendChild(statusEl);
				}
				var banner = byId('hostname-skip-banner-' + id);
				if (banner) {
					banner.remove();
				}
			})
			.catch(function (error) {
				setStatusText(statusId, 'Error: ' + error.message);
			});
	}

	// ---- Hostname credentials setup: skip button - POST then navigate,
	// exactly like Classic's skipLogin(). ----

	function performCredentialsSkip(shorthand) {
		setStatusText('setup-credentials-skip-status', 'Skipping...');
		window
			.quasarrApiFetch('/api/credentials/' + shorthand + '/skip', { method: 'POST' })
			.then(function (response) {
				if (!response.ok) {
					throw new Error('Failed to skip login');
				}
				window.location.href = '/skip-success';
			})
			.catch(function (error) {
				setStatusText('setup-credentials-skip-status', error.message);
			});
	}

	// ---- FlareSolverr setup: skip button - POST then navigate, exactly
	// like Classic's skipFlaresolverr(). ----

	function performFlaresolverrSkip() {
		setStatusText('setup-flaresolverr-skip-status', 'Skipping...');
		window
			.quasarrApiFetch('/api/flaresolverr/skip', { method: 'POST' })
			.then(function (response) {
				if (!response.ok) {
					throw new Error('Failed to skip flaresolverr-next setup');
				}
				window.location.href = '/skip-success';
			})
			.catch(function (error) {
				setStatusText('setup-flaresolverr-skip-status', error.message);
			});
	}

	// ---- JDownloader setup: verify credentials, then reveal the device
	// picker - the one genuinely dynamic step (device list comes from a
	// live My.JDownloader lookup, so it cannot be a plain form submit). ----

	function performJdVerify(button) {
		var userField = byId('jd-user');
		var passField = byId('jd-pass');
		var user = userField ? userField.value : '';
		var pass = passField ? passField.value : '';

		if (button) {
			button.disabled = true;
			button.textContent = 'Verifying...';
		}
		setStatusText('setup-jd-verify-status', '');

		window
			.quasarrApiFetch('/api/verify_jdownloader', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ user: user, pass: pass })
			})
			.then(function (response) {
				return response.json();
			})
			.then(function (data) {
				if (!data.success || !Array.isArray(data.devices) || data.devices.length === 0) {
					throw new Error(data.message || 'Could not verify credentials.');
				}
				var select = byId('jd-device');
				if (select) {
					select.innerHTML = '';
					data.devices.forEach(function (device) {
						var option = document.createElement('option');
						option.value = device;
						option.textContent = device;
						select.appendChild(option);
					});
				}
				var hiddenUser = byId('jd-hidden-user');
				var hiddenPass = byId('jd-hidden-pass');
				if (hiddenUser) {
					hiddenUser.value = user;
				}
				if (hiddenPass) {
					hiddenPass.value = pass;
				}
				// The visible fields stay in the DOM but only the hidden
				// copies above are ever submitted (POST /api/store_jdownloader
				// reads user/pass by name, not jd-user/jd-pass) - disabling
				// them here prevents an edit made after verification from
				// silently being ignored, since the Verify button (the only
				// way to refresh the hidden copies) is about to be hidden too.
				if (userField) {
					userField.disabled = true;
				}
				if (passField) {
					passField.disabled = true;
				}
				var deviceTile = byId('setup-jd-device-tile');
				if (deviceTile) {
					deviceTile.hidden = false;
				}
				if (button) {
					button.hidden = true;
				}
			})
			.catch(function (error) {
				setStatusText('setup-jd-verify-status', error.message);
				if (button) {
					button.disabled = false;
					button.textContent = 'Verify Credentials';
				}
			});
	}

	// ---- Shared double-submit guard for every native setup <form> (marked
	// with the boolean data-guard-submit attribute) - matches Classic's
	// per-page formSubmitted-flag + button-disable pattern. A second submit
	// of the SAME form (button click or Enter-key implicit submission) is
	// cancelled outright; the triggering submitter (if any) is disabled and
	// relabelled one macrotask later via setTimeout(0) so the browser has
	// already read its name/value (e.g. the *arr selector's
	// name="client" value="radarr") into the pending submission first -
	// disabling a named submit button synchronously inside its own 'submit'
	// handler would drop that value from the request. ----

	var guardedForms = typeof WeakSet === 'function' ? new WeakSet() : null;

	function onSetupFormSubmit(event) {
		var form = event.target;
		if (!(form instanceof HTMLFormElement) || !form.hasAttribute('data-guard-submit')) {
			return;
		}
		if (!guardedForms) {
			return;
		}
		if (guardedForms.has(form)) {
			event.preventDefault();
			return;
		}
		guardedForms.add(form);

		var submitter = event.submitter;
		if (submitter instanceof HTMLButtonElement) {
			window.setTimeout(function () {
				submitter.disabled = true;
				submitter.textContent = 'Saving...';
			}, 0);
		}
	}

	function onSetupFlowsClick(event) {
		var eventTarget = event.target instanceof Element ? event.target : null;
		var actionElement = eventTarget ? eventTarget.closest('[data-action]') : null;
		if (!actionElement) {
			return;
		}

		var action = actionElement.getAttribute('data-action');
		switch (action) {
			case 'setup-hostnames-import':
				performSetupHostnamesImport();
				break;
			case 'setup-hostname-toggle-details':
				toggleSetupHostnameDetails(
					actionElement,
					actionElement.getAttribute('data-hostname-id') || ''
				);
				break;
			case 'setup-hostname-credentials-check':
				checkSetupHostnameCredentials(
					actionElement.getAttribute('data-hostname-id') || ''
				);
				break;
			case 'setup-credentials-skip':
				performCredentialsSkip(actionElement.getAttribute('data-shorthand') || '');
				break;
			case 'setup-flaresolverr-skip':
				performFlaresolverrSkip();
				break;
			case 'setup-jd-verify':
				performJdVerify(actionElement);
				break;
			default:
				break;
		}
	}

	document.addEventListener('DOMContentLoaded', function () {
		document.addEventListener('click', onSetupFlowsClick);
		document.addEventListener('submit', onSetupFormSubmit);
	});
})();

(function bootstrapCarbonTime() {
	'use strict';

	function nowSeconds() {
		return Math.floor(Date.now() / 1000);
	}

	function formatDuration(totalSeconds) {
		var remaining = Math.max(0, Math.floor(totalSeconds));
		var days = Math.floor(remaining / 86400);
		var hours = Math.floor((remaining % 86400) / 3600);
		var minutes = Math.floor((remaining % 3600) / 60);
		var seconds = remaining % 60;
		var clock = [hours, minutes, seconds]
			.map(function (value) {
				return String(value).padStart(2, '0');
			})
			.join(':');
		return days ? days + 'd ' + clock : clock;
	}

	// The ONE parser every ticking countdown goes through, whichever deadline
	// attribute a given element carries: a cohort deadline takes precedence
	// over a retry deadline when both are present on the same element, so a
	// card carrying only a cohort deadline keeps counting down instead of
	// collapsing to zero on the first tick. Never a second read, never a
	// selector built from rendered package data.
	function deferredCountdownEpoch(element) {
		var epoch = Number.parseInt(
			element.dataset.cohortDeadlineEpoch ?? element.dataset.retryAfterEpoch ?? '0',
			10
		);
		return Number.isFinite(epoch) ? epoch : 0;
	}

	function updateDeferredCountdowns(root) {
		var scope = root || document;
		var now = nowSeconds();
		scope.querySelectorAll('.deferred-countdown').forEach(function (element) {
			var remaining = Math.max(0, deferredCountdownEpoch(element) - now);
			element.textContent = formatDuration(remaining);
		});
	}

	function relativePhrase(diffSeconds) {
		if (Math.abs(diffSeconds) < 45) {
			return 'just now';
		}
		var duration = formatDuration(Math.abs(diffSeconds));
		return diffSeconds >= 0 ? 'in ' + duration : duration + ' ago';
	}

	// Upgrades every plain `<time data-epoch>` deadline (currently the
	// Statistics page's Filecrypt sweep deadline) from a fixed UTC string to
	// local time plus a relative phrase. Never touches `.deferred-countdown`
	// elements - those carry a different attribute pair and have their own
	// dedicated ticking formatter above.
	function upgradeEpochTimes(root) {
		var scope = root || document;
		scope.querySelectorAll('time[data-epoch]').forEach(function (element) {
			if (element.classList.contains('deferred-countdown')) {
				return;
			}
			var epoch = Number.parseInt(element.dataset.epoch || '0', 10);
			if (!Number.isFinite(epoch) || epoch <= 0) {
				return;
			}
			var localText = new Date(epoch * 1000).toLocaleString();
			element.textContent = localText + ' (' + relativePhrase(epoch - nowSeconds()) + ')';
		});
	}

	window.CarbonTime = {
		nowSeconds: nowSeconds,
		formatDuration: formatDuration,
		deferredCountdownEpoch: deferredCountdownEpoch,
		updateDeferredCountdowns: updateDeferredCountdowns,
		upgradeEpochTimes: upgradeEpochTimes
	};

	document.addEventListener('DOMContentLoaded', function () {
		upgradeEpochTimes();
		window.setInterval(upgradeEpochTimes, 30000);
	});
})();

(function bootstrapCarbonDownloads() {
	'use strict';

	var REFRESH_INTERVAL_MS = 5000;
	var SLOW_THRESHOLD_MS = 5000;
	var SCROLL_STORAGE_KEY = 'quasarr_downloads_scroll_y';
	var COLLAPSE_STORAGE_KEY = 'otherPackagesOpen';
	// Mirrors EVIDENCE_THRESHOLD in quasarr/providers/crypter_cooldowns.py:
	// a provisional hold needs three distinct observations before the
	// crypter itself cools down. Shown so "1" reads as "1 of 3" rather than
	// as a bare counter with no scale.
	var EVIDENCE_TARGET = 3;

	var refreshTimer = null;
	var isFetching = false;
	var refreshPaused = false;
	var modalObserver = null;
	var lastOtherTotal = 0;

	var DEFERRED_STATE_LABELS = {
		observing: 'Observing',
		cooldown: 'Cooldown',
		probe_queued: 'Probe queued'
	};
	// Status TONES are the four semantic dot colors (`.cds-status--*`), used
	// wherever a live state is shown as a dot; TAG tones are the pill palette
	// (`.cds-tag--*`), used for closed classifications (category, final
	// history result). The two vocabularies are deliberately separate - a
	// running download is a state, a category is a label.
	var DEFERRED_STATE_TONES = {
		observing: 'info',
		cooldown: 'warning',
		probe_queued: 'info'
	};
	var QUEUE_STATUS_LABELS = {
		waiting_captcha: 'Waiting for CAPTCHA',
		downloading: 'Downloading',
		extracting: 'Extracting',
		queued: 'Queued'
	};
	var QUEUE_STATUS_TONES = {
		waiting_captcha: 'warning',
		downloading: 'success',
		extracting: 'success',
		queued: 'info'
	};
	// The bar fill follows the work itself rather than the dot: a paused or
	// queued package is not "healthy green" progress, and a package waiting
	// for a CAPTCHA carries the same warning color as its dot.
	var QUEUE_BAR_TONES = {
		waiting_captcha: 'warning',
		downloading: 'interactive',
		extracting: 'success',
		queued: 'interactive'
	};
	var HISTORY_STATUS_LABELS = {
		completed: 'Completed',
		failed: 'Failed'
	};
	var HISTORY_STATUS_TONES = {
		completed: 'green',
		failed: 'red'
	};
	// Category tags. An unknown or custom category (including "not_quasarr")
	// falls back to the neutral gray pill rather than borrowing a meaning.
	var CATEGORY_TONES = {
		movies: 'blue',
		tv: 'purple',
		music: 'teal',
		docs: 'teal',
		anime: 'teal',
		books: 'teal'
	};

	function byId(id) {
		return document.getElementById(id);
	}

	function buildEl(tagName, className, text) {
		var el = document.createElement(tagName);
		if (className) {
			el.className = className;
		}
		if (text !== undefined) {
			el.textContent = text;
		}
		return el;
	}

	function queueStatusLabel(status) {
		return QUEUE_STATUS_LABELS[status] || String(status || '');
	}

	// A `.cds-status` indicator: a colored dot plus optional visible text.
	// The dot itself is decorative (`aria-hidden`) - callers that render it
	// without visible text must give it an accessible name of their own.
	function buildStatusDot(tone, text) {
		var span = buildEl('span', 'cds-status cds-status--' + tone);
		var dot = buildEl('span', 'cds-status__dot');
		dot.setAttribute('aria-hidden', 'true');
		span.appendChild(dot);
		if (text) {
			span.appendChild(document.createTextNode(text));
		}
		return span;
	}

	// A `.cds-progress` bar with the value kept readable as text beside it.
	// Carries the same accessibility contract the Statistics ratio bars have
	// (role plus aria-valuemin/max/now and an identifying aria-label), so a
	// screen reader announces which package is at which percentage instead
	// of meeting a decorative div. `label` is optional - the Dashboard
	// preview shows its percentage in its own meta line already.
	function buildProgress(pct, tone, label, ariaLabel) {
		var value = Math.max(0, Math.min(100, Number(pct) || 0));
		var wrap = buildEl('div', 'cds-progress-cell');
		var bar = buildEl('div', 'cds-progress');
		bar.setAttribute('role', 'progressbar');
		bar.setAttribute('aria-valuemin', '0');
		bar.setAttribute('aria-valuemax', '100');
		bar.setAttribute('aria-valuenow', String(Math.round(value)));
		bar.setAttribute('aria-label', String(ariaLabel || 'Progress'));
		var fill = buildEl('div', 'cds-progress__fill cds-progress__fill--' + tone);
		fill.style.width = value + '%';
		bar.appendChild(fill);
		wrap.appendChild(bar);
		if (label) {
			wrap.appendChild(buildEl('span', 'cds-progress-cell__label', label));
		}
		return wrap;
	}

	function escapeForModal(value) {
		var element = document.createElement('div');
		element.textContent = String(value ?? '');
		return element.innerHTML;
	}

	function appendTextCell(row, value) {
		var cell = document.createElement('td');
		cell.textContent = value == null ? '' : String(value);
		row.appendChild(cell);
	}

	// Mirrors quasarr/providers/carbon_icons.py's reviewed "trash-can" and
	// "unlocked" icons (same Carbon source + sha256, recorded there) - the
	// two this file draws itself, while the bulk toolbar's "renew" is
	// rendered server-side by render_icon() and needs no mirror here.
	// Duplicated as FIXED, non-interpolated markup (this
	// object never carries package data; only buildActionIcon()'s closed
	// `name` lookup touches it) because render_icon() only runs server-side
	// and these row/bulk actions are built entirely client-side. Parsed via
	// a detached wrapper's innerHTML rather than createElementNS(), because
	// the SVG XML namespace URI createElementNS() would need is itself a
	// web-address-shaped string literal, which this file's no-remote-URL
	// contract forbids - the HTML parser assigns a bare `<svg>` tag the
	// correct SVG namespace on its own, so no namespace URI string is
	// needed at all.
	var ROW_ACTION_ICON_MARKUP = {
		'trash-can':
			'<svg viewBox="0 0 32 32" fill="currentColor" aria-hidden="true" focusable="false" class="cds-icon cds-icon--sm">' +
			'<rect x="12" y="12" width="2" height="12"/><rect x="18" y="12" width="2" height="12"/>' +
			'<path d="M4,6V8H6V28a2,2,0,0,0,2,2H24a2,2,0,0,0,2-2V8h2V6ZM8,28V8H24V28Z"/>' +
			'<rect x="12" y="2" width="8" height="2"/></svg>',
		unlocked:
			'<svg viewBox="0 0 32 32" fill="currentColor" aria-hidden="true" focusable="false" class="cds-icon cds-icon--sm">' +
			'<path d="M24,14H12V8a4,4,0,0,1,8,0h2A6,6,0,0,0,10,8v6H8a2,2,0,0,0-2,2V28a2,2,0,0,0,2,2H24a2,2,0,0,0,2-2V16A2,2,0,0,0,24,14Zm0,14H8V16H24Z"/></svg>'
	};

	function buildActionIcon(name) {
		var markup = ROW_ACTION_ICON_MARKUP[name];
		if (!markup) {
			return null;
		}
		var wrapper = document.createElement('div');
		wrapper.innerHTML = markup;
		return wrapper.firstElementChild;
	}

	function iconNameForAction(action) {
		switch (action) {
			case 'package-delete':
				return 'trash-can';
			default:
				return null;
		}
	}

	function buttonTextForAction(action) {
		switch (action) {
			case 'deferred-probe-one':
				return 'Check';
			case 'deferred-remove-one':
				return 'Remove';
			case 'package-delete':
				return 'Delete package and files';
			default:
				return 'Action';
		}
	}

	// Icon-only row action: the icon is decorative (aria-hidden), so the
	// button's `aria-label`/`title` (both set to `label`) are its whole
	// accessible name and its whole visible tooltip - both must lead with
	// the fixed action phrase (see the call sites' "<Action phrase>: <name>"
	// convention) so a screen-reader user scanning many rows hears the
	// action before the item name, matching label-in-name (WCAG 2.5.3).
	function buildActionButton(action, label, extraClass) {
		var button = document.createElement('button');
		button.type = 'button';
		button.className = 'cds-icon-button' + (extraClass ? ' ' + extraClass : '');
		button.setAttribute('data-action', action);
		button.setAttribute('aria-label', label);
		button.setAttribute('title', label);
		var icon = buildActionIcon(iconNameForAction(action));
		if (icon) {
			button.appendChild(icon);
		} else {
			button.textContent = buttonTextForAction(action);
		}
		return button;
	}

	// `variant` picks the presentation only: 'inline' is the text link the
	// queue row puts directly under the release name, anything else the icon
	// button the deferred row groups with its other actions. The target, the
	// accessible name and the tooltip are built here either way, so the two
	// presentations can never drift apart.
	// A labelled row action. The deferred table has room for words, and two
	// named buttons read faster than two icons whose meaning has to be
	// learned first. The accessible name still adds the package, so the
	// control reads "Check" on screen and "Check now: <release>" to a screen
	// reader - the visible words are contained in the accessible name, which
	// is what label-in-name (WCAG 2.5.3) asks for.
	function buildTextActionButton(action, label, variantClass) {
		var button = document.createElement('button');
		button.type = 'button';
		button.className = 'cds-btn ' + variantClass + ' cds-btn--compact';
		button.setAttribute('data-action', action);
		button.setAttribute('aria-label', label);
		button.setAttribute('title', label);
		button.textContent = buttonTextForAction(action);
		return button;
	}

	function buildCaptchaLink(packageId, name, variant) {
		var inline = variant === 'inline';
		var link = document.createElement('a');
		link.className = inline ? 'cds-release__action' : 'cds-icon-button';
		link.href = '/captcha?package_id=' + encodeURIComponent(packageId);
		var label = 'Solve CAPTCHA: ' + String(name || 'package');
		link.setAttribute('aria-label', label);
		link.title = label;
		var icon = inline ? null : buildActionIcon('unlocked');
		if (icon) {
			link.appendChild(icon);
			return link;
		}
		link.textContent = inline ? 'Solve CAPTCHA →' : 'Solve CAPTCHA';
		return link;
	}

	function buildCountdownRow(label, attribute, epoch) {
		var wrapper = document.createElement('div');
		var labelEl = document.createElement('span');
		labelEl.className = 'cds-field__help';
		labelEl.textContent = label + ' ';
		var timeEl = document.createElement('time');
		timeEl.className = 'deferred-countdown';
		timeEl.setAttribute(attribute, String(epoch || 0));
		timeEl.textContent = window.CarbonTime.formatDuration(0);
		wrapper.appendChild(labelEl);
		wrapper.appendChild(timeEl);
		return wrapper;
	}

	// ---- Row builders (one implementation, shared by the initial load and
	// every 5s poll - server-rendered markup never diverges from this because
	// the Python renderer ships only the empty loading skeleton). ----

	function buildDeferredRow(row) {
		var tr = document.createElement('tr');
		tr.dataset.packageId = row.package_id;
		tr.dataset.packageName = row.name;

		var selectCell = document.createElement('td');
		var checkbox = document.createElement('input');
		checkbox.type = 'checkbox';
		checkbox.className = 'deferred-select';
		checkbox.value = row.package_id;
		checkbox.setAttribute('aria-label', 'Select ' + String(row.name || 'package'));
		selectCell.appendChild(checkbox);
		tr.appendChild(selectCell);

		var nameCell = document.createElement('td');
		nameCell.appendChild(buildEl('p', 'cds-release', row.name));
		nameCell.appendChild(
			buildEl('p', 'cds-field__help', row.crypter_label + ' · ' + row.reason_label)
		);
		tr.appendChild(nameCell);

		var stateCell = document.createElement('td');
		stateCell.appendChild(
			buildStatusDot(
				DEFERRED_STATE_TONES[row.state] || 'info',
				DEFERRED_STATE_LABELS[row.state] || String(row.state || 'Unknown')
			)
		);
		tr.appendChild(stateCell);

		// Inside a sweep the package's own evidence counter is meaningless -
		// the cohort's tested/total is what decides its fate - so the column
		// reports whichever of the two currently governs this package.
		var evidenceCell = document.createElement('td');
		evidenceCell.appendChild(
			buildEl(
				'span',
				'cds-mono',
				row.cohort_total > 0
					? row.cohort_tested + ' / ' + row.cohort_total
					: row.evidence_count + ' / ' + EVIDENCE_TARGET
			)
		);
		tr.appendChild(evidenceCell);

		var nextCheckCell = document.createElement('td');
		nextCheckCell.appendChild(buildCountdownRow('Retry', 'data-retry-after-epoch', row.retry_after_epoch));
		if (row.cohort_total > 0 || row.cohort_deadline_epoch > 0) {
			nextCheckCell.appendChild(
				buildCountdownRow('Cohort deadline', 'data-cohort-deadline-epoch', row.cohort_deadline_epoch)
			);
		}
		tr.appendChild(nextCheckCell);

		var sweepCell = document.createElement('td');
		sweepCell.appendChild(
			buildProgress(
				row.cohort_total > 0 ? (100 * row.cohort_tested) / row.cohort_total : 0,
				'interactive',
				row.cohort_total > 0 ? row.cohort_tested + ' / ' + row.cohort_total : '—',
				'Sweep progress: ' + row.name
			)
		);
		tr.appendChild(sweepCell);

		var actionsCell = document.createElement('td');
		actionsCell.className = 'cds-row-actions';
		actionsCell.appendChild(
			buildTextActionButton('deferred-probe-one', 'Check now: ' + row.name, 'cds-btn--ghost')
		);
		if (row.can_solve_captcha) {
			actionsCell.appendChild(buildCaptchaLink(row.package_id, row.name));
		}
		actionsCell.appendChild(
			buildTextActionButton(
				'deferred-remove-one',
				'Remove pending package: ' + row.name,
				'cds-btn--danger-ghost'
			)
		);
		tr.appendChild(actionsCell);

		return tr;
	}

	function buildQueueRow(row) {
		var tr = document.createElement('tr');
		tr.dataset.packageId = row.package_id;
		tr.dataset.packageName = row.name;

		// The status is a dot, not a column of repeated words. The dot is
		// decorative, so the label lives on twice: as the cell's tooltip for
		// a pointer, and as visually hidden text for the accessible name a
		// screen reader reads out with the rest of the row.
		var statusLabel = queueStatusLabel(row.status);
		var statusCell = document.createElement('td');
		var dot = buildStatusDot(QUEUE_STATUS_TONES[row.status] || 'info', '');
		dot.setAttribute('title', statusLabel);
		dot.appendChild(buildEl('span', 'cds-visually-hidden', statusLabel));
		statusCell.appendChild(dot);
		tr.appendChild(statusCell);

		var nameCell = document.createElement('td');
		nameCell.appendChild(buildEl('p', 'cds-release', row.name));
		if (row.can_solve_captcha) {
			// The one action a waiting package actually needs sits with the
			// release name instead of hiding among the row's icon buttons.
			nameCell.appendChild(buildCaptchaLink(row.package_id, row.name, 'inline'));
		}
		tr.appendChild(nameCell);

		var categoryCell = document.createElement('td');
		categoryCell.appendChild(
			buildEl(
				'span',
				'cds-tag cds-tag--' + (CATEGORY_TONES[row.category] || 'gray'),
				row.category
			)
		);
		tr.appendChild(categoryCell);

		appendTextCell(tr, row.size_label);

		var etaCell = document.createElement('td');
		etaCell.appendChild(buildEl('span', 'cds-mono', row.eta_unknown ? '—' : row.eta));
		tr.appendChild(etaCell);

		var progressCell = document.createElement('td');
		progressCell.appendChild(
			buildProgress(
				row.percentage,
				QUEUE_BAR_TONES[row.status] || 'interactive',
				String(row.percentage) + '%',
				'Download progress: ' + row.name
			)
		);
		tr.appendChild(progressCell);

		var actionsCell = document.createElement('td');
		actionsCell.className = 'cds-row-actions';
		actionsCell.appendChild(
			buildActionButton(
				'package-delete',
				'Delete package and files: ' + row.name,
				'cds-icon-button--danger'
			)
		);
		tr.appendChild(actionsCell);

		return tr;
	}

	function buildHistoryRow(row) {
		var tr = document.createElement('tr');
		tr.dataset.packageId = row.package_id;
		tr.dataset.packageName = row.name;

		// History is a finished classification, not a live state, so it
		// leads with a tag rather than a dot - and leads the row, because
		// "did this work?" is the question a history table answers first.
		var statusCell = document.createElement('td');
		statusCell.appendChild(
			buildEl(
				'span',
				'cds-tag cds-tag--' + (HISTORY_STATUS_TONES[row.status] || 'gray'),
				HISTORY_STATUS_LABELS[row.status] || String(row.status || '')
			)
		);
		tr.appendChild(statusCell);

		var nameCell = document.createElement('td');
		nameCell.appendChild(buildEl('p', 'cds-release', row.name));
		if (row.status === 'failed' && row.error) {
			nameCell.appendChild(buildEl('p', 'cds-release__error', row.error));
		}
		tr.appendChild(nameCell);

		var categoryCell = document.createElement('td');
		categoryCell.appendChild(
			buildEl(
				'span',
				'cds-tag cds-tag--' + (CATEGORY_TONES[row.category] || 'gray'),
				row.category
			)
		);
		tr.appendChild(categoryCell);

		appendTextCell(tr, row.size_label);

		var actionsCell = document.createElement('td');
		actionsCell.className = 'cds-row-actions';
		actionsCell.appendChild(
			buildActionButton(
				'package-delete',
				'Delete package and files: ' + row.name,
				'cds-icon-button--danger'
			)
		);
		tr.appendChild(actionsCell);

		return tr;
	}

	// ---- Selection: snapshot/restore by value only, never by a selector
	// built from package data (see restoreDeferredSelection). ----

	function selectedDeferredPackageIds() {
		return Array.from(document.querySelectorAll('.deferred-select:checked'), function (checkbox) {
			return checkbox.value;
		});
	}

	function selectedDeferredNames() {
		return Array.from(document.querySelectorAll('.deferred-select:checked'), function (checkbox) {
			var row = checkbox.closest('tr');
			return row ? row.dataset.packageName : '';
		});
	}

	// Matches by value instead of a built selector, so a package ID can never
	// reach a query, and IDs that stopped being rendered stay dropped.
	function restoreDeferredSelection(packageIds) {
		var selected = new Set(packageIds);
		if (!selected.size) {
			return;
		}
		document.querySelectorAll('.deferred-select').forEach(function (checkbox) {
			if (selected.has(checkbox.value)) {
				checkbox.checked = true;
			}
		});
	}

	function updateDeferredToolbarState() {
		var selected = selectedDeferredPackageIds();
		var allBoxes = document.querySelectorAll('.deferred-select');
		var probeBtn = document.querySelector('[data-action="deferred-probe-selected"]');
		var removeBtn = document.querySelector('[data-action="deferred-remove-selected"]');
		var countEl = byId('deferred-selection-count');
		var disabled = selected.length === 0;

		if (probeBtn) {
			probeBtn.disabled = disabled;
		}
		if (removeBtn) {
			removeBtn.disabled = disabled;
		}
		if (countEl) {
			countEl.textContent = selected.length + ' selected';
		}

		var selectAll = byId('deferred-select-all');
		if (selectAll) {
			selectAll.checked = allBoxes.length > 0 && selected.length === allBoxes.length;
			selectAll.indeterminate = selected.length > 0 && selected.length < allBoxes.length;
		}
	}

	// ---- Search: the input lives outside #downloads-content (Python's
	// _downloads_toolbar()), so its value/focus survive every poll. Filtering
	// is reapplied after each rebuild instead of relying on it surviving the
	// DOM replacement. ----

	function applySearchFilter() {
		var input = byId('downloads-search');
		var term = input ? input.value.trim().toLowerCase() : '';
		document.querySelectorAll('#downloads-content tbody tr[data-package-name]').forEach(function (row) {
			row.hidden = term.length > 0 && row.dataset.packageName.toLowerCase().indexOf(term) === -1;
		});
	}

	// ---- Scroll and collapse persistence (ported from the Classic Downloads
	// page's refreshContent()/restoreCollapseState()). ----

	function saveScrollPosition() {
		try {
			window.sessionStorage.setItem(SCROLL_STORAGE_KEY, String(window.scrollY || 0));
		} catch (_error) {
			// Scroll position simply doesn't persist when storage is unavailable.
		}
	}

	function restoreScrollPosition() {
		var saved;
		try {
			saved = Number(window.sessionStorage.getItem(SCROLL_STORAGE_KEY) || '0');
		} catch (_error) {
			saved = 0;
		}
		if (!Number.isFinite(saved)) {
			return;
		}
		window.scrollTo(0, saved);
	}

	// Rebuilds the summary text from `lastOtherTotal` (the live count
	// renderOtherTables() just computed) via DOM nodes, never a single
	// `summary.textContent = <string>` assignment - that used to destroy
	// the `#downloads-other-count` span permanently, so byId() found it
	// only on the very first call and every later renderOtherTables() call
	// silently stopped updating the visible count. `countEl` is re-fetched
	// (and recreated if somehow still missing) on every call instead of
	// being cached, so the count always has a live element to write into.
	function updateOtherSummary() {
		var summary = byId('otherPackagesSummary');
		var details = byId('otherPackagesDetails');
		if (!summary || !details) {
			return;
		}
		var countEl = byId('downloads-other-count');
		if (!countEl) {
			countEl = document.createElement('span');
			countEl.id = 'downloads-other-count';
		}
		countEl.textContent = String(lastOtherTotal);
		var plural = lastOtherTotal === 1 ? '' : 's';
		summary.textContent = '';
		summary.appendChild(document.createTextNode(details.open ? 'Hide ' : 'Show '));
		summary.appendChild(countEl);
		summary.appendChild(document.createTextNode(' other package' + plural));
	}

	function restoreCollapseState() {
		var details = byId('otherPackagesDetails');
		if (!details) {
			return;
		}
		var stored = null;
		try {
			stored = window.localStorage.getItem(COLLAPSE_STORAGE_KEY);
		} catch (_error) {
			stored = null;
		}
		if (stored === 'true') {
			details.open = true;
		}
		updateOtherSummary();
	}

	// Mirrors Classic's `deleted=` handling (quasarr/api/packages/__init__.py):
	// strip the query param from the URL bar via history.replaceState so a
	// reload/share never repeats the one-time status, and auto-hide the
	// banner after 5 seconds.
	function clearDeletedQueryParamAndScheduleBannerHide() {
		if (!window.location.search.includes('deleted=')) {
			return;
		}
		try {
			var url = new URL(window.location.href);
			url.searchParams.delete('deleted');
			window.history.replaceState({}, '', url);
		} catch (_error) {
			// URL rewriting is best-effort; the banner still auto-hides below.
		}
		var banner = byId('downloads-status-banner');
		if (banner) {
			window.setTimeout(function () {
				banner.hidden = true;
			}, 5000);
		}
	}

	function wireCollapsePersistence() {
		var details = byId('otherPackagesDetails');
		if (!details) {
			return;
		}
		details.addEventListener('toggle', function () {
			try {
				window.localStorage.setItem(COLLAPSE_STORAGE_KEY, String(details.open));
			} catch (_error) {
				// State simply doesn't persist across reloads.
			}
			restoreCollapseState();
		});
	}

	// ---- Modal pause/resume: refresh is paused for the duration of every
	// destructive-action confirmation dialog and resumed the moment it
	// closes, whichever way it closes. Confirm, Cancel, Escape, and backdrop
	// click do NOT share one call site - the base shell's Cancel/Escape/
	// backdrop paths all call its own private closeModalInternal() directly,
	// never the exported window.closeModal() - so the only path-independent
	// signal is the modal's own `hidden` attribute. A MutationObserver on it
	// resumes on every close, however it happened, without depending on
	// which internal function the base shell chose for that path. ----

	function pauseRefresh() {
		if (refreshTimer) {
			window.clearTimeout(refreshTimer);
			refreshTimer = null;
		}
		refreshPaused = true;
	}

	function resumeRefresh() {
		refreshPaused = false;
		loadDownloads();
	}

	function installModalResumeHook() {
		var modal = byId('cds-modal');
		if (!modal || modalObserver) {
			return;
		}
		var wasHidden = modal.hidden;
		modalObserver = new MutationObserver(function () {
			if (modal.hidden && !wasHidden) {
				resumeRefresh();
			}
			wasHidden = modal.hidden;
		});
		modalObserver.observe(modal, { attributes: true, attributeFilter: ['hidden'] });
	}

	// ---- Deferred bulk/single actions ----

	function deferredActionEndpoint(kind) {
		return kind === 'probe'
			? { url: '/api/packages/deferred/probe', method: 'POST', successKey: 'requested' }
			: { url: '/api/packages/deferred', method: 'DELETE', successKey: 'deleted' };
	}

	function showDeferredActionResult(result, successKey) {
		var statusEl = byId('deferred-action-status');
		if (!statusEl) {
			return;
		}
		var rejected = Array.isArray(result && result.rejected) ? result.rejected : [];
		if (rejected.length) {
			statusEl.textContent = rejected
				.map(function (item) {
					return String(item.package_id) + ': ' + String(item.reason);
				})
				.join('; ');
			return;
		}
		if (result && result.success === false) {
			statusEl.textContent = String(result.message || 'Request failed');
			return;
		}
		var completed = Array.isArray(result && result[successKey]) ? result[successKey].length : 0;
		statusEl.textContent = completed + ' package' + (completed === 1 ? '' : 's') + ' updated';
	}

	async function runDeferredAction(packageIds, kind) {
		var statusEl = byId('deferred-action-status');
		var ids = (packageIds || []).filter(Boolean);
		if (!ids.length) {
			if (statusEl) {
				statusEl.textContent = 'Select at least one deferred package';
			}
			return;
		}
		var endpoint = deferredActionEndpoint(kind);
		try {
			var response = await window.quasarrApiFetch(endpoint.url, {
				method: endpoint.method,
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ package_ids: ids })
			});
			var result = await response.json();
			showDeferredActionResult(result, endpoint.successKey);
		} catch (_error) {
			if (statusEl) {
				statusEl.textContent = 'Deferred package request failed';
			}
		}
		await loadDownloads();
	}

	// D1: deferred protected-row removal is a distinct, non-destructive-to-disk
	// action - it has never started downloading, so the confirmation names
	// neither file deletion nor irreversibility, and the wording is always
	// "Remove pending package(s)", never "Delete package and files".
	function confirmRemovePending(packageIds, names) {
		var ids = (packageIds || []).filter(Boolean);
		if (!ids.length) {
			var statusEl = byId('deferred-action-status');
			if (statusEl) {
				statusEl.textContent = 'Select at least one deferred package';
			}
			return;
		}
		var title = ids.length === 1 ? 'Remove pending package?' : 'Remove ' + ids.length + ' pending packages?';
		var nameList = (names || [])
			.filter(Boolean)
			.map(escapeForModal)
			.join('</p><p class="modal-package-name">');
		var body =
			'<p class="modal-package-name">' +
			(nameList || 'Selected packages') +
			'</p><p>This removes the pending package' +
			(ids.length === 1 ? '' : 's') +
			' from Quasarr’s queue. ' +
			(ids.length === 1 ? 'It has' : 'They have') +
			' not started downloading, so no files exist on disk for ' +
			(ids.length === 1 ? 'it' : 'them') +
			'.</p>';
		var actions =
			'<button class="cds-btn cds-btn--secondary" type="button" data-action="modal-close">Cancel</button>' +
			'<button class="cds-btn cds-btn--danger" type="button" id="downloads-confirm-remove">Remove pending package' +
			(ids.length === 1 ? '' : 's') +
			'</button>';

		pauseRefresh();
		window.showModal(title, body, actions, { eyebrow: 'Downloads' });
		var confirmBtn = byId('downloads-confirm-remove');
		if (confirmBtn) {
			confirmBtn.addEventListener('click', function onConfirm() {
				confirmBtn.removeEventListener('click', onConfirm);
				window.closeModal();
				runDeferredAction(ids, 'remove');
			});
		}
	}

	// D1: ordinary queue/history deletion keeps calling delete_package() via
	// the existing /packages/delete/<id> redirect route (unchanged) - the
	// confirmation explicitly names disk deletion and irreversibility.
	function confirmDeletePackage(packageId, name) {
		var body =
			'<p class="modal-package-name">' +
			escapeForModal(name) +
			'</p>' +
			'<section class="cds-notification cds-notification--warning" role="alert">' +
			'<p class="cds-notification__message"><strong>Files are deleted too.</strong> The package is ' +
			'removed from JDownloader together with downloaded files. This cannot be undone.</p>' +
			'</section>';
		var actions =
			'<button class="cds-btn cds-btn--secondary" type="button" data-action="modal-close">Cancel</button>' +
			'<button class="cds-btn cds-btn--danger" type="button" id="downloads-confirm-delete">Delete package and files</button>';

		pauseRefresh();
		window.showModal('Delete package and files?', body, actions, { eyebrow: 'Downloads' });
		var confirmButton = byId('downloads-confirm-delete');
		if (confirmButton) {
			confirmButton.addEventListener('click', function onConfirm() {
				confirmButton.removeEventListener('click', onConfirm);
				var url = '/packages/delete/' + encodeURIComponent(packageId);
				if (name) {
					url += '?title=' + encodeURIComponent(name);
				}
				window.location.href = url;
			});
		}
	}

	function rowDisplayName(row) {
		return row ? row.dataset.packageName || '' : '';
	}

	// ---- Rendering ----

	function updateEmptyMessage(id, isEmpty, emptyText) {
		var el = byId(id);
		if (!el) {
			return;
		}
		el.hidden = !isEmpty;
		if (isEmpty) {
			el.textContent = emptyText;
		}
	}

	function renderDeferredTable(rows) {
		var tbody = byId('deferred-table-body');
		if (!tbody) {
			return;
		}
		// Snapshotted immediately before the tbody is cleared, not before the
		// fetch that produced `rows` - a selection made while that fetch was
		// still in flight (the race window widens exactly on a slow
		// connection) must still be captured here, not the stale pre-fetch
		// state.
		var selectedIds = selectedDeferredPackageIds();
		tbody.textContent = '';
		rows.forEach(function (row) {
			tbody.appendChild(buildDeferredRow(row));
		});
		updateEmptyMessage('deferred-empty-message', rows.length === 0, 'No deferred packages.');
		restoreDeferredSelection(selectedIds);
		updateDeferredToolbarState();
	}

	// The Queue tile heading carries its own count, so the number stays
	// readable while the table itself scrolls out of view.
	function updateQueueCount(total) {
		var countEl = byId('queue-count');
		if (countEl) {
			countEl.textContent = '(' + total + ')';
		}
	}

	function renderQueueTable(rows) {
		var tbody = byId('queue-table-body');
		if (!tbody) {
			return;
		}
		tbody.textContent = '';
		rows.forEach(function (row) {
			tbody.appendChild(buildQueueRow(row));
		});
		updateQueueCount(rows.length);
		updateEmptyMessage('queue-empty-message', rows.length === 0, 'No active downloads.');
	}

	function renderHistoryTable(rows) {
		var tbody = byId('history-table-body');
		if (!tbody) {
			return;
		}
		tbody.textContent = '';
		rows.forEach(function (row) {
			tbody.appendChild(buildHistoryRow(row));
		});
		updateEmptyMessage('history-empty-message', rows.length === 0, 'No history yet.');
	}

	function renderOtherTables(otherQueueRows, otherHistoryRows) {
		var section = byId('downloads-other-section');
		var total = otherQueueRows.length + otherHistoryRows.length;
		lastOtherTotal = total;
		if (section) {
			section.hidden = total === 0;
		}

		var otherQueueBody = byId('other-queue-table-body');
		if (otherQueueBody) {
			otherQueueBody.textContent = '';
			otherQueueRows.forEach(function (row) {
				otherQueueBody.appendChild(buildQueueRow(row));
			});
		}
		var otherHistoryBody = byId('other-history-table-body');
		if (otherHistoryBody) {
			otherHistoryBody.textContent = '';
			otherHistoryRows.forEach(function (row) {
				otherHistoryBody.appendChild(buildHistoryRow(row));
			});
		}
		restoreCollapseState();
	}

	function renderDisconnected() {
		updateEmptyMessage('deferred-empty-message', true, 'JDownloader is not connected.');
		updateEmptyMessage('queue-empty-message', true, 'JDownloader is not connected.');
		updateEmptyMessage('history-empty-message', true, 'JDownloader is not connected.');
		var deferredBody = byId('deferred-table-body');
		if (deferredBody) {
			deferredBody.textContent = '';
		}
		var queueBody = byId('queue-table-body');
		if (queueBody) {
			queueBody.textContent = '';
		}
		var historyBody = byId('history-table-body');
		if (historyBody) {
			historyBody.textContent = '';
		}
		var otherSection = byId('downloads-other-section');
		if (otherSection) {
			otherSection.hidden = true;
		}
		updateQueueCount(0);
		updateDeferredToolbarState();
	}

	function renderDownloads(data) {
		var content = byId('downloads-content');
		if (!content) {
			return;
		}

		if (!data || data.connected === false) {
			renderDisconnected();
		} else {
			renderDeferredTable(Array.isArray(data.deferred) ? data.deferred : []);
			renderQueueTable(Array.isArray(data.queue) ? data.queue : []);
			renderHistoryTable(Array.isArray(data.history) ? data.history : []);
			renderOtherTables(
				Array.isArray(data.other_queue) ? data.other_queue : [],
				Array.isArray(data.other_history) ? data.other_history : []
			);
		}

		content.dataset.state = 'loaded';
		window.CarbonTime.updateDeferredCountdowns(content);
		applySearchFilter();
		restoreScrollPosition();
		window.requestAnimationFrame(restoreScrollPosition);
	}

	// ---- Poll cycle ----

	async function loadDownloads() {
		if (refreshPaused || isFetching) {
			return;
		}
		isFetching = true;
		var startTime = Date.now();
		var warningEl = byId('downloads-slow-warning');
		var slowTimer = window.setTimeout(function () {
			if (warningEl) {
				warningEl.hidden = false;
			}
		}, SLOW_THRESHOLD_MS);

		saveScrollPosition();

		try {
			var response = await window.quasarrApiFetch('/api/packages/list');
			var elapsed = Date.now() - startTime;
			window.clearTimeout(slowTimer);
			if (warningEl) {
				warningEl.hidden = elapsed < SLOW_THRESHOLD_MS;
			}
			if (response.ok) {
				var data = await response.json();
				renderDownloads(data);
			}
		} catch (_error) {
			window.clearTimeout(slowTimer);
		} finally {
			isFetching = false;
			if (!refreshPaused) {
				if (refreshTimer) {
					window.clearTimeout(refreshTimer);
				}
				refreshTimer = window.setTimeout(loadDownloads, REFRESH_INTERVAL_MS);
			}
		}
	}

	// ---- Delegated event wiring (E1: no inline handlers, data-action only) ----

	function onDownloadsClick(event) {
		var target = event.target instanceof Element ? event.target : null;
		var actionElement = target ? target.closest('[data-action]') : null;
		if (!actionElement) {
			return;
		}

		var action = actionElement.getAttribute('data-action');
		var row = actionElement.closest('tr');

		switch (action) {
			case 'deferred-probe-one':
				if (row) {
					runDeferredAction([row.dataset.packageId], 'probe');
				}
				break;
			case 'deferred-remove-one':
				if (row) {
					confirmRemovePending([row.dataset.packageId], [rowDisplayName(row)]);
				}
				break;
			case 'deferred-probe-selected':
				runDeferredAction(selectedDeferredPackageIds(), 'probe');
				break;
			case 'deferred-remove-selected':
				confirmRemovePending(selectedDeferredPackageIds(), selectedDeferredNames());
				break;
			case 'package-delete':
				if (row) {
					confirmDeletePackage(row.dataset.packageId, rowDisplayName(row));
				}
				break;
			default:
				break;
		}
	}

	function onDownloadsChange(event) {
		var target = event.target;
		if (!(target instanceof Element)) {
			return;
		}
		if (target.id === 'deferred-select-all') {
			document.querySelectorAll('.deferred-select').forEach(function (checkbox) {
				checkbox.checked = target.checked;
			});
			updateDeferredToolbarState();
			return;
		}
		if (target.classList.contains('deferred-select')) {
			updateDeferredToolbarState();
		}
	}

	function onDownloadsInput(event) {
		var target = event.target;
		if (target instanceof Element && target.id === 'downloads-search') {
			applySearchFilter();
		}
	}

	document.addEventListener('DOMContentLoaded', function () {
		var content = byId('downloads-content');
		if (!content) {
			return;
		}

		installModalResumeHook();
		wireCollapsePersistence();
		clearDeletedQueryParamAndScheduleBannerHide();
		document.addEventListener('click', onDownloadsClick);
		document.addEventListener('change', onDownloadsChange);
		document.addEventListener('input', onDownloadsInput);
		window.addEventListener('scroll', saveScrollPosition, { passive: true });
		window.setInterval(function () {
			window.CarbonTime.updateDeferredCountdowns(content);
		}, 1000);

		restoreScrollPosition();
		loadDownloads();
	});
})();
