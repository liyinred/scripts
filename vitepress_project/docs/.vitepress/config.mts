import { defineConfig } from 'vitepress'

export default defineConfig({
  lang: 'zh-CN',
  base: '/help/',
  outDir: '../dist_docs',
  title: '小合云帮助中心',
  description: '项目文档站点',
  locales: {
    root: {
      label: '简体中文',
      lang: 'zh-CN',
      title: '小合云帮助中心',
      description: '项目文档站点'
    },
    en: {
      label: 'English',
      lang: 'en-US',
      link: '/en/',
      title: 'Project Docs',
      description: 'Project documentation site',
      themeConfig: {
        docFooter: {
          prev: 'Previous page',
          next: 'Next page'
        },
        outline: {
          label: 'On this page'
        },
        darkModeSwitchLabel: 'Appearance',
        lightModeSwitchTitle: 'Switch to light theme',
        darkModeSwitchTitle: 'Switch to dark theme',
        sidebarMenuLabel: 'Menu',
        returnToTopLabel: 'Return to top',
        langMenuLabel: 'Change language',
        skipToContentLabel: 'Skip to content'
      }
    }
  },
  themeConfig: {
    i18nRouting: false,
    langMenuLabel: '切换语言',
    docFooter: {
      prev: '上一页',
      next: '下一页'
    },
    outline: {
      label: '页面导航'
    },
    darkModeSwitchLabel: '外观',
    lightModeSwitchTitle: '切换至浅色模式',
    darkModeSwitchTitle: '切换至深色模式',
    sidebarMenuLabel: '菜单',
    returnToTopLabel: '返回顶部',
    skipToContentLabel: '跳转到内容',
    search: {
      provider: 'local',
      options: {
        translations: {
          button: {
            buttonText: '搜索',
            buttonAriaLabel: '搜索文档'
          },
          modal: {
            displayDetails: '显示详细列表',
            resetButtonTitle: '清空搜索',
            backButtonTitle: '关闭搜索',
            noResultsText: '未找到结果',
            footer: {
              selectText: '选择',
              selectKeyAriaLabel: '回车',
              navigateText: '导航',
              navigateUpKeyAriaLabel: '上箭头',
              navigateDownKeyAriaLabel: '下箭头',
              closeText: '关闭',
              closeKeyAriaLabel: 'Esc'
            }
          }
        },
        locales: {
          en: {
            translations: {
              button: {
                buttonText: 'Search',
                buttonAriaLabel: 'Search docs'
              },
              modal: {
                displayDetails: 'Display detailed list',
                resetButtonTitle: 'Reset search',
                backButtonTitle: 'Close search',
                noResultsText: 'No results for',
                footer: {
                  selectText: 'to select',
                  selectKeyAriaLabel: 'enter',
                  navigateText: 'to navigate',
                  navigateUpKeyAriaLabel: 'up arrow',
                  navigateDownKeyAriaLabel: 'down arrow',
                  closeText: 'to close',
                  closeKeyAriaLabel: 'escape'
                }
              }
            }
          }
        }
      }
    },

    sidebar: [
      // {
      //   text: '帮助中心',
      //   items: [
      //     { text: '简介', link: '/' }
      //   ]
      // },
      {
        text: '用户协议',
        collapsed: false,
        items: [
          { text: '共享计划用户协议', link: '/agreements/user-agreement' },
          { text: '计费方式及 SLA 规则', link: '/agreements/billing-sla' }
        ]
      },
      {
        text: '对账结算',
        collapsed: false,
        items: [
          { text: '企业用户发票开具须知', link: '/settlements/invoices_info' }
        ]
      }
    ]
  }
})
