import React, { useEffect, useRef, useState } from "react";
import { Channel, ChannelTypePerson } from "wukongimjssdk";
import {
  ArrowLeft,
  PanelLeftClose,
  PanelLeftOpen,
  PanelRightClose,
  PanelRightOpen,
} from "lucide-react";
import WKApp from "../../App";
import { t } from "../../i18n";
import { ChatContentPage } from "../Chat";
import { WORKSPACE_MENU_ID } from "../../features/workspace/workspaceNavigation";
import {
  WORKSPACE_SPLITTER_MAX_WIDTH,
  WORKSPACE_SPLITTER_RATIO,
} from "../../Components/WKLayout/layoutWidth";
import "./index.css";

/**
 * 工作区页（`/workspace?bot=<uid>[&session=...][&path=...]`）。
 *
 * 渲染模型与 mail / mcp-market 生效先例一致 —— 本页是宿主 WKLayout 的
 * **左栏**（MainContentLeft 经 route.get("/workspace") 挂载）：内嵌同源
 * workbench iframe；与该 bot 的 1v1 会话挂在宿主**右栏**
 * （`WKApp.routeRight.replaceToRoot(<ChatContentPage/>)`），不再自绘第二列。
 *
 * 路由核心（Route/RoutePath）零改动；页面只消费 `window.location.search`，
 * 不参与路由匹配。菜单激活 / openWorkspaceUrl 重入都经事件驱动本页重读
 * URL 并重建右侧会话（组件在 historyRoutePaths 模型下长期挂载、不 remount）。
 */

interface WorkspaceQuery {
  bot: string;
  session: string;
  path: string;
}

/**
 * 折叠编排写在宿主 `.wk-layout` 根上的 class（本页在 WKLayout 左栏内，
 * 经 closest 上溯即可命中）。右栏是经 routeRight 挂载的宿主面板，
 * 用 CSS display:none 隐藏而非 unmount，避免破坏聊天状态；
 * 左栏宽度用 !important 压过 WKLayout 的内联 width。
 */
const WK_LAYOUT_RIGHT_COLLAPSED_CLASS = "wk-workspace-right-collapsed";
const WK_LAYOUT_NAVRAIL_COLLAPSED_CLASS = "wk-workspace-navrail-collapsed";

function readWorkspaceQuery(): WorkspaceQuery {
  let search = "";
  try {
    search = window.location.search || "";
  } catch {
    search = "";
  }
  const params = new URLSearchParams(search);
  return {
    bot: params.get("bot") || "",
    session: params.get("session") || "",
    path: params.get("path") || "",
  };
}

function buildWorkbenchSrc(query: WorkspaceQuery): string {
  const params = new URLSearchParams();
  params.set("bot", query.bot);
  if (query.session) params.set("session", query.session);
  if (query.path) params.set("path", query.path);
  return `/workbench/?${params.toString()}`;
}

export default function WorkspacePage() {
  // 每次「进入/重入工作区」事件后自增，驱动重读 URL + 重建右侧会话。
  // 组件在宿主的 historyRoutePaths 模型下长期挂载（display 切换），
  // 不会因菜单切换 remount，必须靠事件自驱动刷新。
  const [revision, setRevision] = useState(0);
  // 面板折叠状态：会话级（不持久化）；离开工作区时自动复位。
  const [chatCollapsed, setChatCollapsed] = useState(false);
  const [navRailCollapsed, setNavRailCollapsed] = useState(false);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const layoutRootRef = useRef<Element | null>(null);
  const query = readWorkspaceQuery();
  // 挂载即意味着工作区是当前激活菜单（宿主只会渲染激活菜单的路由）。
  const wasActiveRef = useRef(true);

  // 右侧会话栏：把与该 bot 的 1v1 会话挂进宿主右栏。
  // 延迟一拍执行：冷加载时本页 effect 先于右栏 WKViewQueue 的
  // onContext 绑定（setReplaceToRoot 赋值），需等挂载编排完成。
  useEffect(() => {
    if (!query.bot) return;
    const channel = new Channel(query.bot, ChannelTypePerson);
    const timer = window.setTimeout(() => {
      if (typeof WKApp.routeRight.setReplaceToRoot === "function") {
        WKApp.routeRight.replaceToRoot(
          <ChatContentPage key={channel.getChannelKey()} channel={channel} />
        );
      }
    }, 0);
    return () => window.clearTimeout(timer);
  }, [revision, query.bot]);

  // 折叠态 → 宿主 `.wk-layout` 根 class 同步。
  // 组件在 historyRoutePaths 模型下长期挂载，离开工作区不会 unmount；
  // 复位靠 onMenuChanged 里的 setState，卸载兜底在 cleanup 里摘除 class。
  useEffect(() => {
    if (rootRef.current && !layoutRootRef.current) {
      layoutRootRef.current = rootRef.current.closest(".wk-layout");
    }
    const layout = layoutRootRef.current;
    if (!layout) return;
    layout.classList.toggle(WK_LAYOUT_RIGHT_COLLAPSED_CLASS, chatCollapsed);
    layout.classList.toggle(
      WK_LAYOUT_NAVRAIL_COLLAPSED_CLASS,
      navRailCollapsed
    );
    return () => {
      layout.classList.remove(
        WK_LAYOUT_RIGHT_COLLAPSED_CLASS,
        WK_LAYOUT_NAVRAIL_COLLAPSED_CLASS
      );
    };
  }, [chatCollapsed, navRailCollapsed]);

  // 进入/离开工作区的事件编排：
  //  - wk:active-menu-changed：NavRail 点击、switchToMenuById、浏览器
  //    前进/后退（syncMenuFromBrowserPath）、冷加载菜单解析的统一出口；
  //  - wk:workspace-activated：openWorkspaceUrl 的「已在工作区」重入通知。
  // 离开时把右栏 popToRoot 回宿主基座（与 NavRail 切换的默认语义一致），
  // wasActive 闸门保证其它菜单的 reconcile 事件不会误伤无关会话视图。
  useEffect(() => {
    const bump = () => setRevision((r) => r + 1);
    // 左栏宽度编排（先例：MailSidebar）：工作区需要比会话列表更宽的左栏，
    // 通过 wk:layout-left-width 事件向 WKLayout 申请首选宽度 + 更大的上限；
    // 离开/卸载时发 {width: null} 恢复聊天默认宽度与默认上限（360）。
    const requestWideLeft = () => {
      window.dispatchEvent(
        new CustomEvent("wk:layout-left-width", {
          detail: {
            width: Math.floor(window.innerWidth * 0.5),
            maxWidth: WORKSPACE_SPLITTER_MAX_WIDTH,
            ratio: WORKSPACE_SPLITTER_RATIO,
          },
        })
      );
    };
    const resetLeftWidth = () => {
      window.dispatchEvent(
        new CustomEvent("wk:layout-left-width", { detail: { width: null } })
      );
    };
    // 挂载即激活（宿主只会渲染激活菜单的路由），冷加载直达时也生效。
    requestWideLeft();
    const onMenuChanged = (payload?: { menuId?: string }) => {
      if (payload?.menuId === WORKSPACE_MENU_ID) {
        wasActiveRef.current = true;
        requestWideLeft();
        bump();
        return;
      }
      if (wasActiveRef.current) {
        wasActiveRef.current = false;
        resetLeftWidth();
        // 离开工作区：折叠态全部复位（NavRail 必须自动恢复），
        // state 变更驱动上面的 effect 摘掉 `.wk-layout` 上的 class。
        setChatCollapsed(false);
        setNavRailCollapsed(false);
        if (typeof WKApp.routeRight.setPopToRoot === "function") {
          WKApp.routeRight.popToRoot();
        }
      }
    };
    WKApp.mittBus.on("wk:active-menu-changed", onMenuChanged);
    WKApp.mittBus.on("wk:workspace-activated", bump);
    return () => {
      WKApp.mittBus.off("wk:active-menu-changed", onMenuChanged);
      WKApp.mittBus.off("wk:workspace-activated", bump);
      resetLeftWidth();
    };
  }, []);

  const handleBack = () => {
    // 走宿主的编程式菜单切换（含 mail 守卫与 URL 同步），右栏由上面的
    // active-menu-changed 监听负责还原；无宿主能力时降级为路由 push。
    if (WKApp.switchToMenuById) {
      WKApp.switchToMenuById("chat");
      return;
    }
    WKApp.route.push("/");
  };

  // 缺失 bot：友好空态 + 返回按钮。
  if (!query.bot) {
    return (
      <div ref={rootRef} className="wk-workspace wk-workspace--empty">
        <div className="wk-workspace-empty-card">
          <div className="wk-workspace-empty-glyph" aria-hidden="true">
            <svg width="40" height="40" viewBox="0 0 24 24" fill="none">
              <rect x="3" y="4" width="18" height="16" rx="2.5" stroke="currentColor" strokeWidth="1.6" />
              <path d="M3 9h18" stroke="currentColor" strokeWidth="1.6" />
              <path d="M9.5 9v11" stroke="currentColor" strokeWidth="1.6" />
            </svg>
          </div>
          <div className="wk-workspace-empty-title">
            {t("base.workspace.emptyTitle")}
          </div>
          <div className="wk-workspace-empty-desc">
            {t("base.workspace.emptyDesc")}
          </div>
          <button
            type="button"
            className="wk-workspace-empty-action"
            onClick={handleBack}
          >
            <ArrowLeft size={16} />
            <span>{t("base.workspace.backToChat")}</span>
          </button>
        </div>
      </div>
    );
  }

  const src = buildWorkbenchSrc(query);

  return (
    <div ref={rootRef} className="wk-workspace">
      <div className="wk-workspace-topbar">
        <button
          type="button"
          className="wk-workspace-back"
          onClick={handleBack}
          title={t("base.workspace.backToChat")}
        >
          <ArrowLeft size={18} />
          <span>{t("base.workspace.backToChat")}</span>
        </button>
        <div className="wk-workspace-title">
          {t("base.workspace.title")}
        </div>
        <div className="wk-workspace-topbar-spacer" />
        <div className="wk-workspace-topbar-actions">
          <button
            type="button"
            className="wk-workspace-panel-toggle"
            title={t("base.workspace.toggleNavRail")}
            aria-label={t("base.workspace.toggleNavRail")}
            aria-pressed={navRailCollapsed}
            onClick={() => setNavRailCollapsed((v) => !v)}
          >
            {navRailCollapsed ? (
              <PanelLeftOpen size={18} />
            ) : (
              <PanelLeftClose size={18} />
            )}
          </button>
          <button
            type="button"
            className="wk-workspace-panel-toggle"
            title={t("base.workspace.toggleChatPanel")}
            aria-label={t("base.workspace.toggleChatPanel")}
            aria-pressed={chatCollapsed}
            onClick={() => setChatCollapsed((v) => !v)}
          >
            {chatCollapsed ? (
              <PanelRightOpen size={18} />
            ) : (
              <PanelRightClose size={18} />
            )}
          </button>
        </div>
      </div>

      <div className="wk-workspace-body">
        <div className="wk-workspace-main">
          <iframe
            className="wk-workspace-frame"
            src={src}
            title={t("base.workspace.title")}
          />
        </div>
      </div>
    </div>
  );
}
