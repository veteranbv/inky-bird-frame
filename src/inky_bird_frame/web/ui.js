// Shared presentation helpers. Static markup only: no state, storage or network.
const PATHS = {
  feather:
    '<path d="M20.5 3.5c-5 0-9.5 2-12.5 5.5-2.4 2.8-3.5 6.5-3.7 10.8 4.3-.2 8-1.3 10.8-3.7 3.5-3 5.4-7.6 5.4-12.6z"/><path d="M4.5 19.5C8.5 14 13 9.5 18 6"/>',
  home: '<path d="M12 6.2C10 4.8 7.6 4.2 5 4.2V19c2.6 0 5 .6 7 2 2-1.4 4.4-2 7-2V4.2c-2.6 0-5 .6-7 2z"/><path d="M12 6.2V21"/>',
  birds:
    '<path d="M16.2 4.5c-2.9 0-5 2.1-5 5v2.6c0 .7-.2 1.3-.6 1.9L4 21h6.5c4.9 0 8.4-3.4 8.4-8.3V9.3L21.5 8l-2.4-1.2c-.5-1.4-1.6-2.3-2.9-2.3z"/><circle cx="16.3" cy="7.6" r=".4" fill="currentColor"/>',
  collections: '<path d="M7 3.5h10a1 1 0 0 1 1 1V21l-6-4.2L6 21V4.5a1 1 0 0 1 1-1z"/>',
  frame:
    '<rect x="3.5" y="3" width="17" height="18" rx="1"/><rect x="6.8" y="6.3" width="10.4" height="11.4"/>',
  manage:
    '<path d="M4 8h6.3m4.4 0H20M4 16h2.3m4.4 0H20"/><circle cx="12.9" cy="8" r="2.2"/><circle cx="8.5" cy="16" r="2.2"/>',
  play: '<path d="M9 6.8v10.4l8.6-5.2z"/>',
  alert: '<path d="M12 4.5 2.8 19.5h18.4z"/><path d="M12 10v4"/><path d="M12 16.6v.2"/>',
  account: '<circle cx="12" cy="8" r="3.6"/><path d="M5 20c.8-3.4 3.6-5.2 7-5.2s6.2 1.8 7 5.2"/>',
  key: '<circle cx="8.5" cy="12" r="4"/><path d="M12.5 12h8m-3 0v3.4m-2.6-3.4v2.4"/>',
  bell: '<path d="M6 10a6 6 0 0 1 12 0c0 4 1.5 5.5 2 6H4c.5-.5 2-2 2-6z"/><path d="M10 19.5a2.2 2.2 0 0 0 4 0"/>',
  archive:
    '<rect x="3.5" y="4" width="17" height="4.5" rx="0.8"/><path d="M5.5 8.5V20h13V8.5"/><path d="M10 12.5h4"/>',
  wrench:
    '<path d="M14.8 6.3a4.3 4.3 0 0 0-5.6 5.2L4 16.7 7.3 20l5.2-5.2a4.3 4.3 0 0 0 5.2-5.6L14.9 12 12 9.1z"/>',
  pulse: '<path d="M3 12.5h3.6l2.4-6 4 11 2.4-5h5.6"/>',
};

export function icon(name, className = 'icon') {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('class', className);
  svg.innerHTML = PATHS[name];
  return svg;
}
