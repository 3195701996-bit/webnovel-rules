(function () {
  'use strict';
  window.mountMangaDescription = function (textNode, button, text) {
    let expanded = false;
    let disposed = false;
    textNode.textContent = String(text || '');
    textNode.hidden = !textNode.textContent.trim();
    textNode.classList.remove('expanded');
    button.hidden = true;
    button.setAttribute('aria-expanded', 'false');
    button.textContent = '展开简介';
    function measure() {
      if (disposed) return;
      button.hidden = textNode.hidden || (!expanded && textNode.scrollHeight <= textNode.clientHeight + 1);
    }
    button.onclick = function () {
      expanded = !expanded;
      textNode.classList.toggle('expanded', expanded);
      button.setAttribute('aria-expanded', String(expanded));
      button.textContent = expanded ? '收起简介' : '展开简介';
      measure();
    };
    const observer = typeof ResizeObserver === 'function' ? new ResizeObserver(measure) : null;
    if (observer) observer.observe(textNode);
    window.addEventListener('resize', measure);
    const frame = window.requestAnimationFrame(measure);
    return function () {
      disposed = true;
      if (observer) observer.disconnect();
      window.removeEventListener('resize', measure);
      window.cancelAnimationFrame(frame);
    };
  };
}());
