/**
 * The theme toggle, and the script that beats the first paint.
 *
 * `ThemeScript` runs before React hydrates, so a reader who chose dark never sees
 * a white flash. It cannot be a React effect: an effect runs after paint, which
 * is exactly the frame the flash happens in.
 */

const STORAGE_KEY = "theme"

/** Inlined into <head>. Deliberately plain, tiny, and unable to throw. */
export const THEME_SCRIPT = `(function(){try{
var saved=localStorage.getItem('${STORAGE_KEY}');
var dark=saved?saved==='dark':window.matchMedia('(prefers-color-scheme: dark)').matches;
if(dark)document.documentElement.classList.add('dark');
}catch(e){}})();`

export function ThemeToggle() {
  const toggle = () => {
    const dark = document.documentElement.classList.toggle("dark")
    try {
      localStorage.setItem(STORAGE_KEY, dark ? "dark" : "light")
    } catch {
      // A reader with storage disabled still gets the toggle, just not the memory.
    }
  }

  return (
    <button
      type="button"
      className="btn btn-ghost"
      onClick={toggle}
      aria-label="Toggle dark mode"
      title="Toggle theme"
      style={{ minHeight: "2.25rem", padding: "0 0.5rem" }}
    >
      <span className="theme-light">
        <svg
          xmlns="http://www.w3.org/2000/svg"
          width="16"
          height="16"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden="true"
        >
          <circle cx="12" cy="12" r="4" />
          <path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41" />
        </svg>
      </span>
      <span className="theme-dark">
        <svg
          xmlns="http://www.w3.org/2000/svg"
          width="16"
          height="16"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden="true"
        >
          <path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z" />
        </svg>
      </span>
    </button>
  )
}
