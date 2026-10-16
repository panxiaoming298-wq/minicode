import { api } from "../api/client";
import type { WorktreeOperation } from "../types";

export class WorktreeOperationFailure extends Error {}

export async function waitForWorktree(initial: WorktreeOperation, onProgress?: (value: WorktreeOperation) => void) {
  let operation = initial;
  while (operation.status === "preparing") {
    onProgress?.(operation);
    await new Promise((resolve) => setTimeout(resolve, 800));
    operation = (await api.getWorktreeOperation(operation.operationId)).operation;
  }
  if (operation.status === "failed") throw new WorktreeOperationFailure([operation.error || "工作树操作失败。", ...(operation.files || [])].join("\n"));
  return operation;
}
