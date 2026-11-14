import { useState } from "react";
import { Button } from "../ui";
import { copyText } from "./util";

export function CopyButton({ text, label, primary }: { text: string; label: string; primary?: boolean }) {
  const [copied, setCopied] = useState(false);
  const copy = () => {
    void copyText(text);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };
  return (
    <Button size="sm" variant={primary ? "primary" : "secondary"} onClick={copy}>
      {copied ? "Copied" : label}
    </Button>
  );
}
