// Fels Web — main.js
// Shared front-end helpers. Domain pages may extend these.

// ── Mobile sidebar toggle ──
(function () {
    const toggle = document.getElementById("menu-toggle");
    const sidebar = document.querySelector(".sidebar");
    const overlay = document.getElementById("sidebar-overlay");
    if (!toggle || !sidebar || !overlay) return;

    function openSidebar() {
        sidebar.classList.add("open");
        overlay.classList.add("open");
    }
    function closeSidebar() {
        sidebar.classList.remove("open");
        overlay.classList.remove("open");
    }

    toggle.addEventListener("click", openSidebar);
    overlay.addEventListener("click", closeSidebar);
    sidebar.querySelectorAll("a").forEach(function (a) {
        a.addEventListener("click", closeSidebar);
    });
})();

// ── Filtering a list of tick boxes as you type ──
// <input data-filter-list="#list"> keeps only the list's children whose text
// contains what was typed. A ticked item always stays: what you chose must
// not vanish because you narrowed the list to find the next one. Delegated,
// so a form loaded into a modal later is covered too.
(function () {
    function apply(input) {
        var list = document.querySelector(input.getAttribute("data-filter-list"));
        if (!list) return;
        var needle = input.value.trim().toLowerCase();
        Array.prototype.forEach.call(list.children, function (item) {
            var box = item.querySelector('input[type="checkbox"]');
            var keep = !needle || (box && box.checked) ||
                       item.textContent.toLowerCase().indexOf(needle) !== -1;
            item.hidden = !keep;
        });
    }
    document.addEventListener("input", function (e) {
        var input = e.target;
        if (input && input.matches && input.matches("[data-filter-list]")) apply(input);
    });
    document.addEventListener("change", function (e) {
        // Unticking inside a narrowed list: the item may now be filtered out.
        var box = e.target;
        if (!box || box.type !== "checkbox") return;
        var input = document.querySelector("[data-filter-list]");
        if (input && input.value) apply(input);
    });
})();

// ── Material ripple ──
// Adds a touch ripple to interactive surfaces. Elements need
// position:relative + overflow:hidden (handled in CSS).
(function () {
    function spawn(el, e) {
        var rect = el.getBoundingClientRect();
        var size = Math.max(rect.width, rect.height);
        var x = (e.clientX || rect.left + rect.width / 2) - rect.left - size / 2;
        var y = (e.clientY || rect.top + rect.height / 2) - rect.top - size / 2;
        var r = document.createElement("span");
        r.className = "ripple";
        r.style.width = r.style.height = size + "px";
        r.style.left = x + "px";
        r.style.top = y + "px";
        el.appendChild(r);
        r.addEventListener("animationend", function () { r.remove(); });
    }
    var sel = ".btn, .fab, .sidebar-nav a, .checkbox-item, .logout-btn";
    document.addEventListener("click", function (e) {
        var el = e.target.closest(sel);
        if (el) spawn(el, e);
    });
})();

// ── Flash toasts ──
// The messages laid over the page go on their own: a success after four
// seconds, an error or a warning after eight, so there is time to read it.
// A click on one dismisses it at once. They are outside the page's flow, so
// nothing moves when they go.
(function () {
    function leave(el) {
        if (el.dataset.leaving) return;
        el.dataset.leaving = "1";
        el.classList.add("is-leaving");
        setTimeout(function () { el.remove(); }, 400);
    }
    document.querySelectorAll(".flash-area .alert").forEach(function (el) {
        var wait = el.classList.contains("alert--success") ? 4000 : 8000;
        setTimeout(function () { leave(el); }, wait);
        el.addEventListener("click", function () { leave(el); });
    });
    // A success confirmed inside a page (a saved form) fades the same way.
    document.querySelectorAll(".alert--success").forEach(function (el) {
        if (el.closest(".flash-area")) return;
        setTimeout(function () {
            el.style.transition = "opacity 0.4s";
            el.style.opacity = "0";
            setTimeout(function () { el.remove(); }, 400);
        }, 4000);
    });
})();

// ── Modal dialog layer ──
// Any element with [data-modal-url] opens its target in a dialog instead of
// navigating. The URL returns a bare form partial; submitting it POSTs via
// fetch — JSON success reloads the page (showing the flash + fresh list),
// an HTML response (validation error) is re-injected with inline errors.
// Links keep their href, so without JS everything still works as full pages.
(function () {
    var overlay = null;

    function close() {
        if (!overlay) return;
        document.removeEventListener("keydown", onKey);
        var o = overlay;
        overlay = null;
        o.classList.remove("open");
        setTimeout(function () { o.remove(); }, 200);
    }
    function onKey(e) { if (e.key === "Escape") close(); }

    function build(title) {
        overlay = document.createElement("div");
        overlay.className = "modal-overlay";
        overlay.innerHTML =
            '<div class="modal" role="dialog" aria-modal="true">' +
            '<div class="modal__header"><h2></h2>' +
            // Inline SVG, not a Material Symbols ligature: the icon font is
            // subset to the names found in the templates, so this JS-injected
            // one was silently dropped and the button rendered empty. Drawing
            // the X here keeps the only way out of a modal independent of the
            // font loading, being subset correctly, or being cached.
            '<button class="modal__close" type="button" aria-label="Close">' +
            '<svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true" ' +
            'fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">' +
            '<path d="M6 6l12 12M18 6L6 18"/></svg></button></div>' +
            '<div class="modal__body"><div class="modal__loading">…</div></div></div>';
        overlay.querySelector(".modal__header h2").textContent = title || "";
        document.body.appendChild(overlay);
        requestAnimationFrame(function () { overlay.classList.add("open"); });
        overlay.querySelector(".modal__close").addEventListener("click", close);
        document.addEventListener("keydown", onKey);
    }

    function runScripts(container) {
        container.querySelectorAll("script").forEach(function (old) {
            // A data block (type="application/json") is read by the page's
            // own code, not run: it stays as it is.
            if (old.type && old.type !== "text/javascript" && old.type !== "module") return;
            var s = document.createElement("script");
            s.textContent = old.textContent;
            old.replaceWith(s);
        });
    }

    function bind(body) {
        var form = body.querySelector("form");
        if (!form) return;
        var cancel = body.querySelector("[data-modal-cancel]");
        if (cancel) cancel.addEventListener("click", function (e) { e.preventDefault(); close(); });
        form.addEventListener("submit", function (e) {
            e.preventDefault();
            var btn = form.querySelector("button[type=submit]");
            if (btn) btn.disabled = true;
            fetch(form.action, {
                method: "POST",
                body: new FormData(form),
                headers: { "X-Requested-With": "fetch" }
            }).then(function (r) {
                var ct = r.headers.get("content-type") || "";
                if (ct.indexOf("application/json") >= 0) {
                    return r.json().then(function () { window.location.reload(); });
                }
                return r.text().then(function (html) { inject(html); });
            }).catch(function () { if (btn) btn.disabled = false; });
        });
    }

    function inject(html) {
        if (!overlay) return;
        var body = overlay.querySelector(".modal__body");
        body.innerHTML = html;
        runScripts(body);
        bind(body);
    }

    function open(url, title) {
        build(title);
        fetch(url, { headers: { "X-Requested-With": "fetch" } })
            .then(function (r) { return r.text(); })
            .then(function (html) { inject(html); })
            .catch(function () { close(); window.location = url; });
    }

    document.addEventListener("click", function (e) {
        var trg = e.target.closest("[data-modal-url]");
        if (!trg) return;
        e.preventDefault();
        open(trg.getAttribute("data-modal-url"), trg.getAttribute("data-modal-title"));
    });
})();

// ── Styled confirm dialog (replaces the native confirm) ──
// Any <form data-confirm="message"> shows a styled dialog before submitting.
// Optional: data-confirm-title, data-confirm-ok (button label),
// data-confirm-danger (red confirm button). Exposed as window.confirmDialog.
(function () {
    function dialog(opts) {
        return new Promise(function (resolve) {
            var ov = document.createElement("div");
            ov.className = "modal-overlay confirm-overlay";
            ov.innerHTML =
                '<div class="modal modal--confirm" role="alertdialog" aria-modal="true">' +
                  '<div class="confirm__body">' +
                    '<h2 class="confirm__title"></h2>' +
                    '<p class="confirm__msg"></p>' +
                  '</div>' +
                  '<div class="confirm__actions">' +
                    '<button type="button" class="btn btn--outlined" data-act="cancel"></button>' +
                    '<button type="button" class="btn" data-act="ok"></button>' +
                  '</div>' +
                '</div>';
            var titleEl = ov.querySelector(".confirm__title");
            if (opts.title) { titleEl.textContent = opts.title; } else { titleEl.remove(); }
            ov.querySelector(".confirm__msg").textContent = opts.message || "";
            var ok = ov.querySelector("[data-act=ok]");
            ok.textContent = opts.ok || "OK";
            ok.classList.add(opts.danger ? "btn--danger" : "btn--primary");
            var cancel = ov.querySelector("[data-act=cancel]");
            cancel.textContent = opts.cancel || (window.FW && window.FW.cancel) || "Cancel";

            document.body.appendChild(ov);
            requestAnimationFrame(function () { ov.classList.add("open"); });

            function done(v) {
                document.removeEventListener("keydown", onKey);
                ov.classList.remove("open");
                setTimeout(function () { ov.remove(); }, 200);
                resolve(v);
            }
            function onKey(e) {
                if (e.key === "Escape") done(false);
                else if (e.key === "Enter") { e.preventDefault(); done(true); }
            }
            ov.addEventListener("click", function (e) { if (e.target === ov) done(false); });
            cancel.addEventListener("click", function () { done(false); });
            ok.addEventListener("click", function () { done(true); });
            document.addEventListener("keydown", onKey);
            setTimeout(function () { ok.focus(); }, 40);
        });
    }
    window.confirmDialog = dialog;

    function optsFrom(el) {
        return {
            title: el.getAttribute("data-confirm-title") || "",
            message: el.getAttribute("data-confirm"),
            ok: el.getAttribute("data-confirm-ok") || "OK",
            danger: el.hasAttribute("data-confirm-danger")
        };
    }

    // Whole-form guard: <form data-confirm>. form.submit() bypasses this.
    document.addEventListener("submit", function (e) {
        var form = e.target;
        if (!form || !form.hasAttribute || !form.hasAttribute("data-confirm")) return;
        e.preventDefault();
        e.stopPropagation();
        dialog(optsFrom(form)).then(function (ok) { if (ok) form.submit(); });
    }, true);

    // Per-control guard: a specific <button data-confirm> (e.g. one of several
    // submit buttons) or an <a data-confirm> link. On confirm we replay the
    // original action, keeping the button's name/value as the form submitter.
    document.addEventListener("click", function (e) {
        var el = e.target.closest("button[data-confirm], a[data-confirm]");
        if (!el || el.dataset.confirmed === "1") return;
        e.preventDefault();
        e.stopPropagation();
        dialog(optsFrom(el)).then(function (ok) {
            if (!ok) return;
            el.dataset.confirmed = "1";
            if (el.tagName === "A") window.location = el.getAttribute("href");
            else el.click();
        });
    }, true);
})();

// ── Filter menus ──
// The bar is sent by its Filtrer button, not by each tick: a filter is usually
// several choices, and firing on every one asked the server for pages nobody
// had finished asking for. What is left here is the behaviour a <details> does
// not give on its own -- closing when you click away from it, and opening the
// date picker from the whole pill rather than the few pixels of its icon.
(function () {
    document.querySelectorAll("form.filter-bar--live label.filter-date").forEach(
        function (pill) {
            pill.addEventListener("click", function (e) {
                var input = pill.querySelector('input[type="date"]');
                // A click on the numbers is left alone: that is how a date is
                // typed at the keyboard.
                if (!input || e.target === input) return;
                // showPicker() is recent, and can refuse. When it does, the
                // browser's own icon stays the way in.
                if (typeof input.showPicker !== "function") return;
                e.preventDefault();
                try { input.showPicker(); } catch (err) { input.focus(); }
            });
        });

    // A menu left open would sit over the page it is filtering.
    document.addEventListener("click", function (e) {
        document.querySelectorAll(".filter-bar--live details.filter-dd[open]").forEach(
            function (dd) { if (!dd.contains(e.target)) dd.open = false; });
    });
    document.addEventListener("keydown", function (e) {
        if (e.key !== "Escape") return;
        document.querySelectorAll(".filter-bar--live details.filter-dd[open]").forEach(
            function (dd) { dd.open = false; });
    });
})();

// ── Night mode button ──
// The server knows the theme only once someone has chosen one. Until then the
// head script sets it from the machine, and this brings the button into line --
// otherwise a reader whose computer is already dark would be offered "switch to
// dark", and clicking it would appear to do nothing.
(function () {
    var btn = document.getElementById("theme-toggle");
    if (!btn) return;
    var dark = document.documentElement.getAttribute("data-theme") === "dark";
    var icon = btn.querySelector(".material-symbols-outlined");
    var label = btn.getAttribute(dark ? "data-label-light" : "data-label-dark");
    btn.href = btn.getAttribute(dark ? "data-to-light" : "data-to-dark");
    if (icon) icon.textContent = dark ? "light_mode" : "dark_mode";
    if (label) {
        btn.title = label;
        btn.setAttribute("aria-label", label);
    }
})();

// ── Money fields ──
// Amounts here run to seven and eight digits. Typed bare, 5000000 and 50000000
// are the same shape to the eye, and one gets keyed for the other. So a money
// field groups itself in threes while it is being filled in, exactly as the
// figures elsewhere on the page are grouped.
//
// The separator is a no-break space, and every reader of a money field on the
// server takes it back out again (parse_amount), so what is typed and what is
// saved never disagree. A field with no JavaScript running still works: bare
// digits are read the same way.
(function () {
    var NBSP = "\u00a0";

    function group(digits) {
        // From the right, because that is where the grouping starts.
        var out = "", n = 0;
        for (var i = digits.length - 1; i >= 0; i--) {
            out = digits[i] + out;
            if (++n % 3 === 0 && i > 0) out = NBSP + out;
        }
        return out;
    }

    // A field marked data-money="cents" also takes cents after a comma
    // (2 306 273 785,20): the client's bills carry them. A point typed there
    // is read the same way, but only as the last mark with at most two
    // digits after it, so "1.500.000" still means one and a half million.
    function formatCents(el) {
        var before = el.value;
        var m = before.match(/^(.*?)[,.](\d{0,2})$/);
        var whole = (m ? m[1] : before).replace(/\D/g, "").replace(/^0+(?=\d)/, "");
        var after = group(whole) + (m ? "," + m[2] : "");
        if (after === before) return;
        el.value = after;
        try { el.setSelectionRange(after.length, after.length); } catch (err) { /* not a text field */ }
    }

    function format(el) {
        if (el.dataset.money === "cents") return formatCents(el);
        var before = el.value;
        // Where the caret sits, counted in digits rather than characters: the
        // separators move about, the digits do not.
        var caret = el.selectionStart === null ? before.length : el.selectionStart;
        var digitsBefore = before.slice(0, caret).replace(/\D/g, "").length;

        var digits = before.replace(/\D/g, "").replace(/^0+(?=\d)/, "");
        // A field that may go below zero -- an overdrawn account's opening
        // balance -- keeps a leading minus.
        var minus = el.hasAttribute("data-money-signed") && /^\s*[-−]/.test(before) ? "-" : "";
        var after = minus + group(digits);
        if (after === before) return;
        el.value = after;

        // Put the caret back where the same digit is now.
        if (el.selectionStart === null) return;
        var seen = 0, pos = after.length;
        for (var i = 0; i < after.length; i++) {
            if (seen === digitsBefore) { pos = i; break; }
            if (/\d/.test(after[i])) seen++;
        }
        if (seen === digitsBefore && pos === after.length) pos = after.length;
        try { el.setSelectionRange(pos, pos); } catch (err) { /* not a text field */ }
    }

    function bind(root) {
        (root || document).querySelectorAll("input[data-money]").forEach(function (el) {
            if (el.dataset.moneyBound) return;
            el.dataset.moneyBound = "1";
            el.addEventListener("input", function () { format(el); });
            el.addEventListener("blur", function () { format(el); });
            format(el);   // whatever the server put there, grouped too
        });
    }

    bind(document);
    // A form can arrive after the page has: the modal fetches one and drops it
    // in. Watching for added nodes catches those without the modal code having
    // to know this exists.
    new MutationObserver(function (records) {
        for (var i = 0; i < records.length; i++) {
            if (records[i].addedNodes.length) { bind(document); return; }
        }
    }).observe(document.body, { childList: true, subtree: true });
})();

// Reload: the same address fetched again, as a fresh visit. Never the
// browser's own reload, which would offer to send a form a second time.
(function () {
    var btn = document.getElementById('reload-page');
    if (!btn) return;
    btn.addEventListener('click', function () {
        btn.classList.add('is-spinning');
        location.assign(location.pathname + location.search);
    });
})();

// Retour goes back to where you came from, not to the screen's usual list.
//
// Each page notes, for this tab only, the page that led to it -- address,
// filters and tab included. Saving something on the page reloads it from
// itself, which leaves the note alone; arriving just after a form (a new
// order, an edit page) does not count as coming from it; and coming back
// down a path you went up ("vehicle -> client -> Retour") keeps the older
// note, so two pages never send you back and forth. With no note -- a link
// opened in a new tab, a bookmark -- the button keeps its own address.
(function () {
    var KEY = 'fw.back', MAX = 60;
    var FORM = /\/(new|nouveau|nouvelle|edit|modifier|reception|approvisionner|solde-initial|password|login)(\/|$)/;
    var here = location.pathname;
    var notes = {};
    try { notes = JSON.parse(sessionStorage.getItem(KEY) || '{}') || {}; } catch (e) { notes = {}; }

    function pathOf(url) { return url.split('?')[0].split('#')[0]; }

    var ref = null;
    try {
        if (document.referrer) {
            var r = new URL(document.referrer);
            if (r.origin === location.origin) ref = r.pathname + r.search;
        }
    } catch (e) { ref = null; }

    if (ref) {
        var refPath = pathOf(ref);
        var goingBack = notes[refPath] && pathOf(notes[refPath]) === here;
        if (refPath !== here && !FORM.test(refPath) && !goingBack) {
            delete notes[here];
            notes[here] = ref;
            var keys = Object.keys(notes);
            while (keys.length > MAX) delete notes[keys.shift()];
            try { sessionStorage.setItem(KEY, JSON.stringify(notes)); } catch (e) { /* private mode */ }
        }
    }

    document.addEventListener('click', function (e) {
        if (e.defaultPrevented || e.button !== 0 || e.ctrlKey || e.metaKey || e.shiftKey) return;
        var a = e.target.closest('a[data-back], a[data-modal-cancel]');
        if (!a || a.closest('.modal')) return;
        var to = notes[here];
        if (!to || pathOf(to) === here) return;
        e.preventDefault();
        location.assign(to);
    });
})();

// ── Dropdown filters send themselves ──
// A filter bar's dropdown narrows the list as soon as it changes; the Filtrer
// button stays for the search box and for browsers without JavaScript.
document.addEventListener("change", function (e) {
    var sel = e.target;
    if (sel && sel.matches && sel.matches("select[data-autosubmit]") && sel.form) sel.form.submit();
});

// Date filters send themselves too, once a whole date is picked.
document.addEventListener("change", function (e) {
    var el = e.target;
    if (el && el.matches && el.matches("input[data-autosubmit-date]") && el.form && (!el.value || el.validity.valid)) el.form.submit();
});

// The language menus: close on a click anywhere else or on Escape.
(function () {
    var menus = document.querySelectorAll('details.lang-menu');
    if (!menus.length) return;
    document.addEventListener('click', function (e) {
        menus.forEach(function (m) { if (m.open && !m.contains(e.target)) m.open = false; });
    });
    document.addEventListener('keydown', function (e) {
        if (e.key === 'Escape') menus.forEach(function (m) { m.open = false; });
    });
})();

// The bell: closes on a click anywhere else or on Escape, like any menu;
// redraws its list each time it is opened; and keeps its number current.
//
// The number is asked for every 30 seconds while the page is looked at --
// a few bytes each time, in the background, never holding the page up.
// Nothing is asked while the tab is hidden or the phone is locked; coming
// back asks at once. On a bad connection the misses are let go quietly, and
// after three in a row it waits five minutes before trying again.
(function () {
    var bell = document.querySelector('details.bell');
    if (!bell) return;
    var EVERY = 30000, AFTER_FAILURES = 300000;
    var panel = bell.querySelector('.bell__panel');
    var summary = bell.querySelector('summary');
    var baseTitle = document.title.replace(/^\(\d+\+?\)\s*/, '');
    var timer = null, misses = 0, busy = false;

    function show(n) {
        var badge = summary.querySelector('.bell__count');
        if (n > 0) {
            if (!badge) {
                badge = document.createElement('span');
                badge.className = 'bell__count';
                summary.appendChild(badge);
            }
            badge.textContent = n < 100 ? n : '99+';
            document.title = '(' + (n < 100 ? n : '99+') + ') ' + baseTitle;
        } else {
            if (badge) badge.remove();
            document.title = baseTitle;
        }
    }

    function schedule(delay) {
        clearTimeout(timer);
        if (!document.hidden) timer = setTimeout(check, delay);
    }

    function check() {
        if (busy || document.hidden || !window.fetch) return;
        busy = true;
        fetch(bell.dataset.countUrl, { credentials: 'same-origin', cache: 'no-store',
                                       headers: { 'Accept': 'application/json' } })
            .then(function (r) { if (!r.ok) throw r; return r.json(); })
            .then(function (d) { misses = 0; show(d.count || 0); })
            .catch(function () { misses += 1; })
            .then(function () {
                busy = false;
                schedule(misses >= 3 ? AFTER_FAILURES : EVERY);
            });
    }

    document.addEventListener('visibilitychange', function () {
        if (document.hidden) clearTimeout(timer);
        else { misses = 0; check(); }
    });
    show(parseInt((summary.querySelector('.bell__count') || {}).textContent, 10) || 0);
    schedule(EVERY);

    bell.addEventListener('toggle', function () {
        if (!bell.open || !panel.dataset.panelUrl || !window.fetch) return;
        var here = location.pathname + location.search;
        fetch(panel.dataset.panelUrl + '?from=' + encodeURIComponent(here),
              { credentials: 'same-origin', cache: 'no-store' })
            .then(function (r) { if (!r.ok) throw r; return r.text(); })
            .then(function (html) { panel.innerHTML = html; })
            .catch(function () { /* keep the list drawn with the page */ });
    });
    document.addEventListener('click', function (e) {
        if (bell.open && !bell.contains(e.target)) bell.open = false;
    });
    document.addEventListener('keydown', function (e) {
        if (e.key === 'Escape' && bell.open) bell.open = false;
    });
})();


// ── "The full balance" ──
// A button with data-fill-amount puts that amount, and its currency, in the
// form's amount field: the usual case of a payment is paying what is left.
(function () {
    document.addEventListener("click", function (e) {
        var btn = e.target.closest("[data-fill-amount]");
        if (!btn) return;
        var form = btn.closest("form");
        var amount = form && form.querySelector('[name="amount"]');
        if (!amount) return;
        var cur = form.querySelector('[name="currency"]');
        if (cur && btn.dataset.fillCurrency) cur.value = btn.dataset.fillCurrency;
        amount.value = btn.dataset.fillAmount;
        amount.dispatchEvent(new Event("input", { bubbles: true }));
        amount.focus();
    });
})();


// ── Long forms (templates/_form.html) ──
// The summary at the side is built from the sections (data-toc, the form's
// and any kept after it on the page) and each is ticked once complete; the
// one on screen is marked. The save bar says when something changed, and
// leaving asks first. The error summary's link takes you to its field.
// A section says how it is judged complete with data-check, a test in
// window.formChecks; without one, its required fields must be filled.
window.formChecks = window.formChecks || {};
window.formChecks.always = function () { return true; };
// Every field filled in but those marked data-optional (the settings).
window.formChecks.filled = function (sec) {
    var fields = sec.querySelectorAll(".field:not([data-optional])");
    for (var i = 0; i < fields.length; i++) {
        var radios = fields[i].querySelectorAll("input[type=radio]");
        if (radios.length) {
            if (!fields[i].querySelector("input[type=radio]:checked")) return false;
            continue;
        }
        var el = fields[i].querySelector("input:not([type=hidden]), select, textarea");
        if (el && !el.value.trim()) return false;
    }
    return true;
};
// A list with at least one row (the ports).
window.formChecks.rows = function (sec) { return !!sec.querySelector("[data-row]"); };
(function () {
    var form = document.querySelector("form[data-form-page]");
    if (!form) return;
    var toc = document.querySelector("[data-form-toc]");

    function sections() {
        return Array.prototype.slice.call(document.querySelectorAll("[data-toc]")).filter(function (sec) { return !sec.closest("[hidden]"); });
    }
    function complete(sec) {
        var check = sec.getAttribute("data-check");
        if (check && window.formChecks[check]) return window.formChecks[check](sec);
        var needed = sec.querySelectorAll("input[required], select[required], textarea[required]");
        if (!needed.length) return false;
        for (var i = 0; i < needed.length; i++) {
            var el = needed[i];
            if (el.disabled || el.closest("[hidden]")) continue;
            if (el.type === "radio") {
                if (!sec.querySelector("input[name='" + el.name + "']:checked")) return false;
            } else if (!el.value || (el.validity && !el.validity.valid)) {
                return false;
            }
        }
        return true;
    }
    function tick() {
        if (!toc) return;
        sections().forEach(function (sec) {
            var a = toc.querySelector('[data-for="' + sec.id + '"]');
            if (!a) return;
            var done = complete(sec);
            a.classList.toggle("is-done", done);
            a.querySelector(".form-toc__state").textContent = done ? "check_circle" : "";
        });
    }
    if (toc) {
        toc.innerHTML = "";
        sections().forEach(function (sec) {
            var a = document.createElement("a");
            a.href = "#" + sec.id;
            a.setAttribute("data-for", sec.id);
            var icon = document.createElement("span");
            icon.className = "material-symbols-outlined";
            icon.textContent = sec.getAttribute("data-toc-icon");
            var title = document.createElement("span");
            title.textContent = sec.getAttribute("data-toc-title");
            var state = document.createElement("span");
            state.className = "material-symbols-outlined form-toc__state";
            a.appendChild(icon); a.appendChild(title); a.appendChild(state);
            toc.appendChild(a);
        });
        tick();
        // The section on screen.
        if ("IntersectionObserver" in window) {
            var seen = new IntersectionObserver(function (entries) {
                entries.forEach(function (e) {
                    if (!e.isIntersecting) return;
                    toc.querySelectorAll("a").forEach(function (a) { a.classList.toggle("is-on", a.getAttribute("data-for") === e.target.id); });
                });
            }, { rootMargin: "-30% 0px -60% 0px" });
            sections().forEach(function (s) { seen.observe(s); });
        }
    }

    // Changes not saved yet.
    var dirty = false, note = form.querySelector("[data-dirty-note]");
    function mark() { dirty = true; if (note) note.hidden = false; }
    form.addEventListener("input", function () { mark(); tick(); });
    form.addEventListener("change", function () { mark(); tick(); });
    form.addEventListener("submit", function () { dirty = false; });
    // Annuler leaves on purpose: no question. Captured first, before the
    // link navigates.
    document.addEventListener("click", function (e) {
        if (e.target.closest && e.target.closest("a[data-back], a[data-modal-cancel]")) dirty = false;
    }, true);
    window.addEventListener("beforeunload", function (e) { if (dirty) { e.preventDefault(); e.returnValue = ""; } });

    // The error summary's link: to the field, in the middle of the screen.
    var link = form.querySelector("[data-error-link]");
    if (link) link.addEventListener("click", function (e) {
        var input = document.getElementById(link.getAttribute("href").slice(1));
        if (!input) return;
        e.preventDefault();
        input.scrollIntoView({ block: "center" });
        input.focus({ preventScroll: true });
    });
})();


// ── Tabs on a detail page (the detail_tabs macro) ──
// Each tab is a link to ?tab=key, so the page works without script and the
// server opens the right one. Here a click switches the panel in place and
// writes the tab into the address, so a reload (a modal saving) keeps it.
// The last tab of each page is also remembered for the session: an action
// that comes back to the page without ?tab= (a delete) lands where it was.
(function () {
    document.querySelectorAll("[data-tab-scope]").forEach(function (scope) {
        var nav = scope.querySelector("[data-tabs]");
        if (!nav) return;
        var store = "tab:" + location.pathname;
        function open(key, remember) {
            var panel = scope.querySelector('[data-tab-panel="' + key + '"]');
            if (!panel) return false;
            scope.querySelectorAll("[data-tab-panel]").forEach(function (p) { p.hidden = p !== panel; });
            nav.querySelectorAll("[data-tab]").forEach(function (a) {
                var on = a.dataset.tab === key;
                a.classList.toggle("tab--on", on);
                a.setAttribute("aria-selected", on ? "true" : "false");
            });
            if (remember) {
                var url = new URL(location.href);
                url.searchParams.set("tab", key);
                history.replaceState(history.state, "", url);
                try { sessionStorage.setItem(store, key); } catch (e) { /* private mode */ }
            }
            return true;
        }
        nav.addEventListener("click", function (e) {
            var a = e.target.closest("[data-tab]");
            if (!a || e.ctrlKey || e.metaKey || e.shiftKey) return;   // a new browser tab: let the link do it
            e.preventDefault();
            open(a.dataset.tab, true);
        });
        // Arrow keys move along the tabs, as in any tab list.
        nav.addEventListener("keydown", function (e) {
            if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
            var tabs = Array.prototype.slice.call(nav.querySelectorAll("[data-tab]"));
            var i = tabs.indexOf(document.activeElement);
            if (i < 0) return;
            var next = tabs[(i + (e.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length];
            next.focus();
            open(next.dataset.tab, true);
            e.preventDefault();
        });
        var asked = new URL(location.href).searchParams.get("tab");
        if (asked) {
            try { sessionStorage.setItem(store, asked); } catch (e) { /* private mode */ }
        } else {
            var last = null;
            try { last = sessionStorage.getItem(store); } catch (e) { /* private mode */ }
            if (last) open(last, false);
        }
    });
})();


// ── The cart: "+ Cart" without leaving the page ──
// The button posts in the background; the page then says the vehicle is in
// the cart, the count in the top bar follows, and a note offers the cart.
// Without script the form posts as it is and comes back to the same page.
(function () {
    var toast = null, timer = null;
    function say(message, url, ok) {
        if (toast) toast.remove();
        toast = document.createElement("div");
        toast.className = "cart-toast" + (ok ? "" : " cart-toast--error");
        toast.setAttribute("role", "status");
        var text = document.createElement("span");
        text.textContent = message;
        toast.appendChild(text);
        if (ok && url) {
            var a = document.createElement("a");
            a.href = url;
            a.textContent = document.documentElement.lang === "en" ? "View cart" : document.documentElement.lang === "de" ? "Zum Warenkorb" : "Voir le panier";
            toast.appendChild(a);
        }
        document.body.appendChild(toast);
        clearTimeout(timer);
        timer = setTimeout(function () { if (toast) { toast.remove(); toast = null; } }, 4500);
    }
    document.addEventListener("submit", function (e) {
        var form = e.target.closest("form[data-cart-add]");
        if (!form) return;
        e.preventDefault();
        var btn = form.querySelector("button");
        if (btn) btn.disabled = true;
        fetch(form.action, { method: "POST", body: new FormData(form), headers: { "X-Requested-With": "fetch" } })
            .then(function (r) { return r.json(); })
            .then(function (d) {
                if (d.ok) {
                    // Every copy of this vehicle's button on the page (table and phone card).
                    document.querySelectorAll('form[data-cart-add][action="' + form.getAttribute("action") + '"]').forEach(function (f) {
                        var a = document.createElement("a");
                        a.href = d.url;
                        a.className = "in-cart";
                        a.innerHTML = '<span class="material-symbols-outlined">check_circle</span> ';
                        a.appendChild(document.createTextNode(form.dataset.inCart || ""));
                        f.replaceWith(a);
                    });
                    document.querySelectorAll("[data-cart-count]").forEach(function (n) { n.textContent = d.count; n.hidden = !d.count; });
                } else if (btn) {
                    btn.disabled = false;
                }
                say(d.message, d.url, d.ok);
            })
            .catch(function () { form.submit(); });
    });
})();
