import React, { useEffect, useState } from "react";
import { LayoutDashboard } from "lucide-react";
import { Channel, ChannelTypePerson, ChannelInfo } from "wukongimjssdk";
import { t } from "../../i18n";
import IconClick from "../../Components/IconClick";
import { fetchCurrentImChannelInfo } from "../../im-runtime/currentChannelRuntime";
import { openWorkspaceUrl } from "./workspaceNavigation";

interface WorkspaceEntryButtonProps {
  channel: Channel;
}

/**
 * 聊天头部「工作区」入口按钮。
 *
 * 可见性条件：仅 1v1（ChannelTypePerson）且对端是机器人（`orgData.robot === 1`）。
 * 机器人判定走 `fetchCurrentImChannelInfo`（与 BotDetailModal 同款），在
 * `useEffect` 内异步执行；非机器人或拉取失败返回 null，不渲染按钮。
 */
export default function WorkspaceEntryButton({
  channel,
}: WorkspaceEntryButtonProps) {
  const [isRobot, setIsRobot] = useState(false);

  useEffect(() => {
    let cancelled = false;

    // 仅对 1v1 会话判定；群聊直接隐藏。
    if (channel.channelType !== ChannelTypePerson) {
      setIsRobot(false);
      return;
    }

    fetchCurrentImChannelInfo<Channel, ChannelInfo>(channel)
      .then((info) => {
        if (cancelled) return;
        const orgData = (info as { orgData?: { robot?: number } } | undefined)
          ?.orgData;
        setIsRobot(orgData?.robot === 1);
      })
      .catch(() => {
        if (cancelled) return;
        setIsRobot(false);
      });

    return () => {
      cancelled = true;
    };
  }, [channel.getChannelKey()]);

  if (!isRobot) return null;

  return (
    <IconClick
      size="sm"
      icon={<LayoutDashboard size={20} />}
      data-testid="workspace-entry"
      title={t("base.workspace.title")}
      onClick={() => {
        // 不能用 WKApp.route.push：宿主的 restContent 槽位无渲染消费者，
        // 会出现「URL 变了、UI 不动」的不挂载缺陷；统一走菜单激活路径。
        openWorkspaceUrl(channel.channelID);
      }}
    />
  );
}
