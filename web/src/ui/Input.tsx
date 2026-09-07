import type { InputHTMLAttributes, TextareaHTMLAttributes } from "react";

type InputProps = InputHTMLAttributes<HTMLInputElement> & { invalid?: boolean };

export function Input({ invalid = false, className, ...rest }: InputProps) {
  return (
    <input
      className={["ui-input", invalid && "ui-input--invalid", className].filter(Boolean).join(" ")}
      {...rest}
    />
  );
}

type TextareaProps = TextareaHTMLAttributes<HTMLTextAreaElement> & { invalid?: boolean };

export function Textarea({ invalid = false, className, ...rest }: TextareaProps) {
  return (
    <textarea
      className={["ui-input", invalid && "ui-input--invalid", className].filter(Boolean).join(" ")}
      {...rest}
    />
  );
}
