import { Subscriber } from "wukongimjssdk";
import { describe, expect, it, vi } from "vitest";

import {
  buildChannelSettingMemberSearchIndex,
  createChannelSettingMemberSearch,
  filterChannelSettingMembers,
} from "../channelSettingMemberSearch";

function member(uid: string, name: string, remark?: string): Subscriber {
  return { uid, name, remark, orgData: {} } as Subscriber;
}

describe("channel setting member pinyin search", () => {
  const members = [
    member("weijiaoying", "魏娇莹"),
    member("alice", "Alice", "艾丽丝"),
  ];

  it("matches Chinese, uid, full pinyin and case-insensitive pinyin", () => {
    const index = buildChannelSettingMemberSearchIndex(members);

    expect(filterChannelSettingMembers(index, "魏娇")[0].uid).toBe(
      "weijiaoying"
    );
    expect(filterChannelSettingMembers(index, "weijiao")[0].uid).toBe(
      "weijiaoying"
    );
    expect(filterChannelSettingMembers(index, "WEIJIAO")[0].uid).toBe(
      "weijiaoying"
    );
    expect(filterChannelSettingMembers(index, "alice")[0].uid).toBe("alice");
  });

  it("preserves member order and restores all members for an empty query", () => {
    const index = buildChannelSettingMemberSearchIndex(members);

    expect(filterChannelSettingMembers(index, "")).toEqual(members);
    expect(filterChannelSettingMembers(index, "missing")).toEqual([]);
  });

  it("applies the caller filter before building the reusable search", () => {
    const search = createChannelSettingMemberSearch(
      members,
      (item) => item.uid !== "alice"
    );

    expect(search("weijiao")).toEqual([members[0]]);
    expect(search("alice")).toEqual([]);
  });

  it("converts 10,000 member values once and reuses the index", () => {
    const largeMembers = Array.from({ length: 10_000 }, (_, index) =>
      member(`user-${index}`, index === 9_999 ? "魏娇莹" : `User ${index}`)
    );
    const toPinyin = vi.fn((value: string) =>
      value === "魏娇莹" ? "weijiaoying" : value
    );
    const index = buildChannelSettingMemberSearchIndex(largeMembers, toPinyin);

    for (let count = 0; count < 20; count += 1) {
      expect(filterChannelSettingMembers(index, "weijiao")).toHaveLength(1);
    }

    expect(toPinyin).toHaveBeenCalledTimes(20_000);
  });
});
