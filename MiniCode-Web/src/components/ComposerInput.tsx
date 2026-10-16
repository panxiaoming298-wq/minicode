import { useEffect, useLayoutEffect, useRef, useState } from "react";
import type { RefObject, TextareaHTMLAttributes } from "react";
import { SkillMention } from "./SkillText";

type Props = Omit<TextareaHTMLAttributes<HTMLTextAreaElement>, "value" | "onChange"> & {
  value: string;
  skill: { text: string; name: string } | null;
  textareaRef: RefObject<HTMLTextAreaElement | null>;
  onChange: (value: string) => void;
};

/** A native textarea edits the arguments; the selected Skill is one removable token. */
export function ComposerInput({ value, skill, textareaRef, onChange, onKeyDown, onSelect, onBlur, ...props }: Props) {
  const chipRef = useRef<HTMLSpanElement>(null);
  const [wholeSelection, setWholeSelection] = useState(false);
  const remainder = skill ? value.slice(skill.text.length) : value;
  // Keep the original separator in the wire value, including extra spaces/tabs.
  const separator = skill && /^[ \t]/.test(remainder) ? remainder[0] : "";
  const text = skill ? remainder.slice(separator.length) : value;

  function resize() {
    const textarea = textareaRef.current;
    if (!textarea) return;
    const width = chipRef.current?.getBoundingClientRect().width || 0;
    textarea.style.setProperty("--skill-chip-offset", `${width ? width + 8 : 0}px`);
    textarea.style.height = "0px";
    textarea.style.height = `${Math.min(textarea.scrollHeight, 180)}px`;
    syncScroll();
  }

  function syncScroll() {
    if (chipRef.current && textareaRef.current) {
      chipRef.current.style.transform = `translateY(${-textareaRef.current.scrollTop}px)`;
    }
  }

  useLayoutEffect(resize, [value, skill?.name]);
  useEffect(() => { setWholeSelection(false); }, [value]);
  useEffect(() => {
    const observer = new ResizeObserver(resize);
    if (textareaRef.current) observer.observe(textareaRef.current);
    if (chipRef.current) observer.observe(chipRef.current);
    return () => observer.disconnect();
  }, [skill?.name]);

  function replaceValue(next: string, caret: number) {
    setWholeSelection(false);
    onChange(next);
    requestAnimationFrame(() => {
      textareaRef.current?.focus();
      textareaRef.current?.setSelectionRange(caret, caret);
    });
  }

  function selectingWholeInput() {
    const textarea = textareaRef.current;
    return Boolean(skill && wholeSelection && textarea?.selectionStart === 0 && textarea.selectionEnd === text.length);
  }

  return <div className={`composer-input ${skill ? "has-skill" : ""}`}>
    {skill && <span ref={chipRef} className={`composer-skill-chip ${wholeSelection ? "is-selected" : ""}`}>
      <SkillMention name={skill.name} disabled={props.disabled} onRemove={() => replaceValue(text, 0)} />
    </span>}
    <textarea {...props} ref={textareaRef} value={text}
      onChange={event => {
        const next = event.target.value;
        onChange(skill && !wholeSelection ? skill.text + (separator || (/^\s/.test(next) ? "" : " ")) + next : next);
      }}
      onScroll={syncScroll}
      onPointerDown={() => setWholeSelection(false)}
      onBlur={event => { setWholeSelection(false); onBlur?.(event); }}
      onSelect={event => {
        if (wholeSelection && (event.currentTarget.selectionStart !== 0 || event.currentTarget.selectionEnd !== text.length)) setWholeSelection(false);
        onSelect?.(event);
      }}
      onKeyDown={event => {
        if (skill && !event.nativeEvent.isComposing && event.keyCode !== 229) {
          const textarea = event.currentTarget;
          if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "a") {
            event.preventDefault();
            setWholeSelection(true);
            textarea.select();
            return;
          }
          if ((event.key === "Backspace" || event.key === "Delete") && selectingWholeInput()) {
            event.preventDefault();
            replaceValue("", 0);
            return;
          }
          if (event.key === "Backspace" && textarea.selectionStart === 0 && textarea.selectionEnd === 0) {
            event.preventDefault();
            replaceValue(text, 0);
            return;
          }
          if (["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End", "Escape"].includes(event.key)) setWholeSelection(false);
        }
        onKeyDown?.(event);
      }}
      onCopy={event => {
        if (!selectingWholeInput()) return;
        event.preventDefault();
        event.clipboardData.setData("text/plain", value);
      }}
      onCut={event => {
        if (!selectingWholeInput()) return;
        event.preventDefault();
        event.clipboardData.setData("text/plain", value);
        replaceValue("", 0);
      }}
      onPaste={event => {
        if (!selectingWholeInput() || event.defaultPrevented || event.clipboardData.files.length
          || !event.clipboardData.types.includes("text/plain")) return;
        event.preventDefault();
        const next = event.clipboardData.getData("text/plain");
        replaceValue(next, next.length);
      }}
    />
  </div>;
}
