//! \file
/*
**  Copyright (C) - Triton
**
**  This program is under the terms of the Apache License 2.0.
*/

#ifndef TRITON_EXTERNALLIBS_HPP
#define TRITON_EXTERNALLIBS_HPP



//! The Triton namespace
namespace triton {
/*!
 *  \addtogroup triton
 *  @{
 */
  //! The external libraries namespace
  namespace extlibs {
  /*!
   *  \addtogroup triton
   *  @{
   */

    //! The Capstone library namespace
    namespace capstone {
    /*!
     *  \addtogroup extlibs
     *  @{
     */
      #include <capstone/arm.h>
      #include <capstone/arm64.h>
      #include <capstone/capstone.h>
      #include <capstone/riscv.h>
      #include <capstone/x86.h>

      #if CS_API_MAJOR >= 6
      constexpr auto CS_ARCH_ARM64 = CS_ARCH_AARCH64;
      #endif
    /*! @} End of capstone namespace */
    };

  /*! @} End of extlibs namespace */
  };
/*! @} End of triton namespace */
};

#endif  /* TRITON_EXTERNALLIBS_HPP */
