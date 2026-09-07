import { useState, type ReactNode } from "react";
import { Button } from "./Button";
import { Dialog } from "./Dialog";

type Variant = "primary" | "secondary" | "ghost" | "danger";

/**
 * A button that routes through a confirmation Dialog — the drop-in
 * replacement for `confirm(...) && doThing()`.
 */
export function ConfirmButton({
  label,
  title,
  body,
  confirmLabel = "Confirm",
  variant = "danger",
  size,
  disabled = false,
  onConfirm,
}: {
  label: ReactNode;
  title: string;
  body?: ReactNode;
  confirmLabel?: string;
  variant?: Variant;
  size?: "md" | "sm";
  disabled?: boolean;
  onConfirm: () => void;
}) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <Button variant={variant} size={size} disabled={disabled} onClick={() => setOpen(true)}>
        {label}
      </Button>
      <Dialog
        open={open}
        onClose={() => setOpen(false)}
        title={title}
        actions={[
          { label: "Cancel", variant: "ghost", onClick: () => setOpen(false) },
          {
            label: confirmLabel,
            variant,
            onClick: () => {
              setOpen(false);
              onConfirm();
            },
          },
        ]}
      >
        {body}
      </Dialog>
    </>
  );
}
