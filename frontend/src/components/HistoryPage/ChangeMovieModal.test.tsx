import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../../api/client";
import { ChangeMovieModal } from "./ChangeMovieModal";

const preview = vi.fn();
const change = vi.fn();
const listOps = vi.fn();
const recover = vi.fn();

vi.mock("../../api/client", async (orig) => {
  const actual = await orig<typeof import("../../api/client")>();
  return {
    ...actual,
    previewMovieIdentity: (...a: unknown[]) => preview(...a),
    changeMovieIdentity: (...a: unknown[]) => change(...a),
    listMovieIdentityOperations: (...a: unknown[]) => listOps(...a),
    recoverMovieIdentity: (...a: unknown[]) => recover(...a),
  };
});

const okPreview = {
  job_id: 7,
  ok: true,
  previous: { tmdb_id: 111, title: "Titan", year: 2021, final_path: "/m/Titan (2021)/Titan (2021).mkv" },
  proposed: { tmdb_id: 12345, title: "Titan A.E.", year: 2000, final_path: "/m/Titan A.E. (2000)/x.mkv" },
  moves: [
    { title_id: 1, role: "main", src: "/m/Titan (2021)/Titan (2021).mkv", dst: "/m/Titan A.E. (2000)/Titan A.E. (2000).mkv" },
    { title_id: 2, role: "extra", src: "/m/Titan (2021)/Extras/Extra 1.mkv", dst: "/m/Titan A.E. (2000)/Extras/Extra 1.mkv" },
  ],
  blockers: [],
  warnings: ["1 untracked file(s) in the old folder will stay there"],
  token: "tok",
};

function setup(props: Partial<React.ComponentProps<typeof ChangeMovieModal>> = {}) {
  const onApplied = vi.fn();
  const onClose = vi.fn();
  render(
    <ChangeMovieModal
      jobId={7}
      currentTitle="Titan"
      currentYear={2021}
      currentSource="tmdb+heuristic"
      currentConfidence={0.95}
      currentPath="/m/Titan (2021)/Titan (2021).mkv"
      onApplied={onApplied}
      onClose={onClose}
      {...props}
    />,
  );
  return { onApplied, onClose };
}

async function previewId() {
  fireEvent.change(screen.getByLabelText("TMDB movie ID"), { target: { value: "12345" } });
  fireEvent.click(screen.getByRole("button", { name: /^preview$/i }));
}

describe("ChangeMovieModal", () => {
  beforeEach(() => {
    preview.mockReset();
    change.mockReset();
    listOps.mockReset();
    recover.mockReset();
    listOps.mockResolvedValue([]);
  });

  it("offers recovery actions when a correction is stuck in needs_recovery", async () => {
    listOps.mockResolvedValue([
      { operation_id: "op", job_id: 7, status: "needs_recovery", error: "Rollback incomplete", proposed: {} },
    ]);
    recover.mockResolvedValue({ status: "rolled_back" });
    setup();
    await screen.findByText(/Rollback incomplete/);
    listOps.mockResolvedValue([]);
    fireEvent.click(screen.getByRole("button", { name: /undo correction/i }));
    await waitFor(() => expect(recover).toHaveBeenCalledWith(7, "roll_back"));
    await waitFor(() => expect(screen.queryByText(/Rollback incomplete/)).toBeNull());
  });

  it("shows the current identity and keeps apply disabled until a clean preview", () => {
    setup();
    expect(screen.getByText("Titan (2021)", { selector: "span" })).toBeTruthy();
    expect(screen.getByText(/95%/)).toBeTruthy();
    expect((screen.getByRole("button", { name: /apply movie correction/i }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("previews by TMDB id, lists every move and warning, then applies with the token", async () => {
    preview.mockResolvedValue(okPreview);
    change.mockResolvedValue({ status: "done", operation_id: "op" });
    const { onApplied, onClose } = setup();
    await previewId();
    await screen.findByText(/Titan A\.E\. \(2000\)\/Titan A\.E\. \(2000\)\.mkv/);
    expect(preview).toHaveBeenCalledWith(7, 12345);
    expect(screen.getByText(/Extras\/Extra 1\.mkv/, { selector: "span" })).toBeTruthy();
    expect(screen.getByText(/untracked file/)).toBeTruthy();

    const apply = screen.getByRole("button", { name: /apply movie correction/i }) as HTMLButtonElement;
    expect(apply.disabled).toBe(false);
    fireEvent.click(apply);
    await waitFor(() => expect(change).toHaveBeenCalledWith(7, 12345, "tok"));
    await waitFor(() => expect(onApplied).toHaveBeenCalled());
    expect(onClose).toHaveBeenCalled();
  });

  it("disables apply and shows blockers when the preview is not clean", async () => {
    preview.mockResolvedValue({ ...okPreview, ok: false, blockers: ["A file already exists at the destination: /x"] });
    setup();
    await previewId();
    await screen.findByText(/already exists at the destination/);
    expect((screen.getByRole("button", { name: /apply movie correction/i }) as HTMLButtonElement).disabled).toBe(true);
    expect(change).not.toHaveBeenCalled();
  });

  it("shows the server's message when the preview request fails", async () => {
    preview.mockRejectedValue(new ApiError(404, "Not Found", JSON.stringify({ detail: "TMDB movie 5 was not found" })));
    setup();
    await previewId();
    expect(await screen.findByText("TMDB movie 5 was not found")).toBeTruthy();
  });

  it("keeps the dialog open and reports a stale-preview conflict from apply", async () => {
    preview.mockResolvedValue(okPreview);
    change.mockRejectedValue(new ApiError(409, "Conflict", JSON.stringify({ detail: "The preview is out of date; reload it" })));
    const { onClose } = setup();
    await previewId();
    await screen.findByText(/Extra 1/, { selector: "span" });
    fireEvent.click(screen.getByRole("button", { name: /apply movie correction/i }));
    expect(await screen.findByText(/out of date/)).toBeTruthy();
    expect(onClose).not.toHaveBeenCalled();
  });

  it("rejects a non-numeric id", () => {
    setup();
    fireEvent.change(screen.getByLabelText("TMDB movie ID"), { target: { value: "abc" } });
    expect((screen.getByRole("button", { name: /^preview$/i }) as HTMLButtonElement).disabled).toBe(true);
  });
});
