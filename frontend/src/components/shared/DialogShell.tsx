import * as Dialog from "@radix-ui/react-dialog";
import { X } from "lucide-react";
import type { ReactNode } from "react";
import { cn } from "@/lib/cn";

interface DialogShellProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: ReactNode;
  description?: ReactNode;
  children: ReactNode;
  footer?: ReactNode;
  className?: string;
}

/**
 * Every Dialog wraps this: backdrop, title bar, close button, focus trap and
 * escape handling, one z-index. Radix owns the accessibility so no dialog in
 * this app has to get it right individually.
 */
export function DialogShell({
  open,
  onOpenChange,
  title,
  description,
  children,
  footer,
  className,
}: DialogShellProps) {
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-50 bg-black/70 backdrop-blur-[1px]" />
        <Dialog.Content
          className={cn(
            "fixed left-1/2 top-1/2 z-50 w-[min(92vw,34rem)] -translate-x-1/2 -translate-y-1/2",
            "rounded-lg border border-line bg-surface-2 shadow-2xl",
            className,
          )}
        >
          <div className="flex items-start justify-between gap-4 border-b border-line px-5 py-3">
            <div>
              <Dialog.Title className="text-sm font-semibold text-ink-1">{title}</Dialog.Title>
              {description && (
                <Dialog.Description className="mt-1 text-xs text-ink-2">
                  {description}
                </Dialog.Description>
              )}
            </div>
            <Dialog.Close
              aria-label="Close"
              className="rounded p-1 text-ink-3 hover:bg-surface-3 hover:text-ink-1"
            >
              <X size={16} />
            </Dialog.Close>
          </div>
          <div className="px-5 py-4">{children}</div>
          {footer && (
            <div className="flex justify-end gap-2 border-t border-line px-5 py-3">{footer}</div>
          )}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
