import { createApp } from 'vue'
import { createRouter, createWebHistory } from 'vue-router'
import App from './App.vue'
import './style.css'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', component: () => import('./views/Overview.vue') },
    { path: '/documents', component: () => import('./views/Documents.vue') },
    { path: '/documents/:id', component: () => import('./views/DocumentDetail.vue'), props: true },
    { path: '/review', component: () => import('./views/ReviewQueue.vue') },
    { path: '/records', component: () => import('./views/Records.vue') },
    { path: '/price-files', component: () => import('./views/PriceFiles.vue') },
  ],
})

createApp(App).use(router).mount('#app')
