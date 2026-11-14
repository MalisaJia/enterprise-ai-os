import WKApp from "../../App";

/**
 * 工作区导航辅助。
 *
 * 背景（/workspace 不挂载缺陷的修复核心）：宿主（apps/web 的 Main 页）只会
 * 渲染「NavRail 菜单注册表」能解析出来的路由 —— `MainVM.didMount` /
 * `syncMenuFromBrowserPath` 用 `findMenuForRoute` 把 URL 解析成菜单，菜单的
 * `routePath` 才会进入 `historyRoutePaths`，`MainContentLeft` 才调
 * `WKApp.route.get(routePath)` 挂载组件。单纯 `WKApp.route.push("/workspace")`
 * 只做 history.pushState + 把元素写入 `WKApp.shared.restContent` 槽位，而该槽位
 * 在当前宿主里没有任何渲染消费者 —— URL 变了、页面不动、懒加载 chunk 也不会拉取。
 *
 * 所以所有进入工作区的入口（聊天头部按钮 / Bot 资料弹层）统一走
 * 「激活 workspace 菜单 + 把带 query 的 URL 写回地址栏」：
 *   1) `WKApp.switchToMenuById("workspace")` 触发宿主的菜单切换（含守卫），
 *      让 `/workspace` 进入 `historyRoutePaths` → MainContentLeft 挂载工作区页；
 *      其内部 `syncPath("/workspace")` 会 pushState 一个不带 query 的裸路径；
 *   2) afterSwitch 里用 `replaceState` 把 `?bot=...` 补回当前历史条目
 *      （`syncPath` 经 `normalizeRoutePath` 会剥掉 query，必须事后修复）；
 *   3) emit `wk:workspace-activated`，让已挂载的 WorkspacePage 在「重入」
 *      （currentMenuId 已是 workspace，不产生 active-menu-changed）时也能
 *      重读 URL、重建右侧会话。
 */

/** 与 module.tsx registerWorkspaceRoute 注册的菜单 id 保持一致 */
export const WORKSPACE_MENU_ID = "workspace";

/** 最近一次进入工作区的完整 URL（含 ?bot=...），供 NavRail 点击时恢复 */
let lastWorkspaceUrl = "/workspace";

export function getLastWorkspaceUrl(): string {
  return lastWorkspaceUrl;
}

/**
 * 进入 `/workspace?bot=<uid>` 工作区。
 *
 * @param bot 目标机器人 uid；为空时不做任何事（由页面空态兜底是冷加载场景，
 *            程序化入口一律要求带 bot）。
 */
export function openWorkspaceUrl(bot: string): void {
  if (!bot) return;
  const url = `/workspace?bot=${encodeURIComponent(bot)}`;
  lastWorkspaceUrl = url;

  const repairUrlAndNotify = () => {
    try {
      // switchToMenuById 内部的 syncPath 只推裸 `/workspace`，这里把 query 补回。
      window.history.replaceState({}, "title", url);
    } catch {
      // URL 修复 best-effort，不阻塞页面激活
    }
    WKApp.mittBus.emit("wk:workspace-activated", { url });
  };

  if (WKApp.switchToMenuById) {
    WKApp.switchToMenuById(WORKSPACE_MENU_ID, repairUrlAndNotify);
    return;
  }

  // 无菜单导航能力的宿主降级：只改地址栏（与修复前行为一致，不追求渲染）。
  try {
    window.history.pushState({}, "title", url);
  } catch {
    // noop
  }
}
