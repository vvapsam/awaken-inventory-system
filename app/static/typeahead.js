/* Type a name instead of scrolling a list.

   Progressive by construction: the input is the form's real field, so with
   this file blocked or broken the page is still a text box somebody can type
   a name into and submit. Everything here only fills that box in faster.

   Matching is on word starts as well as the whole string, so "urb" finds
   "Chrizel Urbino" — a surname is what people reach for, and a plain
   "starts with" would never find it. */
(function () {
  function esc(s) {
    return String(s).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }

  function hit(name, q) {
    var lower = name.toLowerCase();
    var at = lower.indexOf(q);
    if (at < 0) { return -1; }
    // A match at the start of the name, or of any word in it, ranks above one
    // buried mid-word: "ric" should offer Ric Flores before Chrizel Urbino.
    if (at === 0) { return 0; }
    return lower[at - 1] === ' ' ? 1 : 2;
  }

  function mark(name, q) {
    var at = name.toLowerCase().indexOf(q);
    if (at < 0) { return esc(name); }
    return esc(name.slice(0, at)) + '<b>' + esc(name.slice(at, at + q.length))
      + '</b>' + esc(name.slice(at + q.length));
  }

  function wire(box) {
    var input = box.querySelector('input');
    var list = box.querySelector('.tahl');
    var data = box.querySelector('.tahd');
    if (!input || !list || !data) { return; }
    var people;
    try { people = JSON.parse(data.textContent) || []; } catch (e) { return; }
    var shown = [];
    var at = -1;

    function close() {
      list.hidden = true;
      list.innerHTML = '';
      shown = []; at = -1;
      input.setAttribute('aria-expanded', 'false');
    }

    function paint() {
      Array.prototype.forEach.call(list.children, function (li, i) {
        li.className = i === at ? 'on' : '';
      });
    }

    function open() {
      var q = input.value.trim().toLowerCase();
      var found = people
        .map(function (n) { return { n: n, r: q ? hit(n, q) : 0 }; })
        .filter(function (x) { return x.r >= 0; })
        .sort(function (a, b) { return a.r - b.r || a.n.localeCompare(b.n); })
        .slice(0, 8);
      shown = found.map(function (x) { return x.n; });
      at = -1;
      if (!shown.length) {
        list.innerHTML = '<li class="none">Nobody by that name.</li>';
        list.hidden = false;
        input.setAttribute('aria-expanded', 'true');
        return;
      }
      list.innerHTML = shown.map(function (n) {
        return '<li role="option">' + mark(n, q) + '</li>';
      }).join('');
      list.hidden = false;
      input.setAttribute('aria-expanded', 'true');
      paint();
    }

    function choose(name) {
      input.value = name;
      close();
      if (box.getAttribute('data-submit') && input.form) {
        input.form.submit();
      }
    }

    input.addEventListener('input', open);
    input.addEventListener('focus', open);
    input.addEventListener('blur', function () { setTimeout(close, 160); });
    input.addEventListener('keydown', function (e) {
      if (list.hidden || !shown.length) { return; }
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault();
        at = (at + (e.key === 'ArrowDown' ? 1 : shown.length - 1)) % shown.length;
        if (at < 0) { at = shown.length - 1; }
        paint();
      } else if (e.key === 'Enter' && at >= 0) {
        e.preventDefault();
        choose(shown[at]);
      } else if (e.key === 'Escape') {
        close();
      }
    });
    list.addEventListener('mousedown', function (e) {
      var li = e.target.closest('li');
      if (!li || li.className === 'none') { return; }
      e.preventDefault();
      choose(shown[Array.prototype.indexOf.call(list.children, li)]);
    });
  }

  function start() {
    Array.prototype.forEach.call(document.querySelectorAll('[data-tah]'), wire);
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
})();
