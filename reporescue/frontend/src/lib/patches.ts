import { ApiError, apiFetch } from "./api";

export type PatchDownload = {
  filename: string;
};

function filenameFromDisposition(header: string | null, fallback: string): string {
  if (!header) return fallback;
  const match = /filename\*=UTF-8''([^;]+)|filename="?([^";]+)"?/i.exec(header);
  let name = (match?.[1] ?? match?.[2] ?? "").trim();
  try {
    name = decodeURIComponent(name);
  } catch {
    /* keep the raw filename when it is not valid percent-encoding */
  }
  return name || fallback;
}

/**
 * Download an investigation's patch attachment from the backend.
 * Uses the authenticated API client; resolves with the saved filename.
 * Downloading never applies the patch.
 */
export async function downloadInvestigationPatch(
  investigationId: string
): Promise<PatchDownload> {
  let response: Response;
  try {
    response = await apiFetch(`/api/investigations/${investigationId}/patch`);
  } catch (error) {
    if (error instanceof ApiError && error.status === 0) {
      throw new ApiError(0, "Could not reach the RepoRescue API. Check your connection and retry.");
    }
    throw error;
  }
  if (response.ok) {
    const blob = await response.blob();
    const fallback = `reporescue-${investigationId.slice(0, 8)}.patch`;
    const filename = filenameFromDisposition(
      response.headers.get("Content-Disposition"),
      fallback
    );
    const url = URL.createObjectURL(blob);
    try {
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = filename;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
    } finally {
      URL.revokeObjectURL(url);
    }
    return { filename };
  }
  if (response.status === 401) {
    throw new ApiError(401, "Your session is no longer valid. Sign in again.");
  }
  if (response.status === 404) {
    throw new ApiError(404, "No downloadable patch is available for this investigation.");
  }
  if (response.status === 422) {
    throw new ApiError(
      422,
      "The stored patch does not match the pinned snapshot, so no file was generated."
    );
  }
  if (response.status === 413) {
    throw new ApiError(413, "The patch exceeds the download size limit.");
  }
  throw new ApiError(response.status, `Patch download failed (HTTP ${response.status}).`);
}
