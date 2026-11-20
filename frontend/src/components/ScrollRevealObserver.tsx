"use client";

import { useEffect } from "react";

export default function ScrollRevealObserver() {
  useEffect(() => {
    const motion = window.matchMedia("(prefers-reduced-motion: reduce)");
    // Never hide content when observation is unavailable or motion is reduced.
    if (motion.matches || !("IntersectionObserver" in window)) return;
    const reduce = () => {
      if (motion.matches) document.documentElement.classList.remove("scroll-reveal-ready");
    };
    motion.addEventListener("change", reduce);
    const observer = new IntersectionObserver((entries) => {
      for (const entry of entries) {
        if (entry.isIntersecting) {
          entry.target.classList.add("is-in-view");
          observer.unobserve(entry.target);
        }
      }
    }, { threshold: 0.12, rootMargin: "0px 0px -36px 0px" });

    const reveal = (element: HTMLElement) => {
      // Application pages can be much taller than the viewport. They must
      // never depend on an intersection threshold to become readable.
      if (element.closest(".dashboard-shell")) element.classList.add("is-in-view");
      else observer.observe(element);
    };
    const observe = (root: ParentNode) => {
      root.querySelectorAll<HTMLElement>("[data-scroll-reveal]:not(.is-in-view)").forEach(reveal);
    };
    document.documentElement.classList.add("scroll-reveal-ready");
    observe(document);
    const mutations = new MutationObserver((records) => {
      for (const record of records) {
        record.addedNodes.forEach((node) => {
          if (node instanceof HTMLElement) {
            if (node.matches("[data-scroll-reveal]")) reveal(node);
            observe(node);
          }
        });
      }
    });
    mutations.observe(document.body, { childList: true, subtree: true });
    return () => {
      motion.removeEventListener("change", reduce);
      mutations.disconnect();
      observer.disconnect();
      document.documentElement.classList.remove("scroll-reveal-ready");
    };
  }, []);

  return null;
}
